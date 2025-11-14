# string.py
"""
Utility module for:
- GPT-based linguistic string analysis
- Google Translate-based translation & language detection
- Selecting the best translation between Google and GPT

Environment variables expected:
- OPENAI_API_KEY               (for OpenAI)
- TRANSLATE_PROJECT_ID or GOOGLE_CLOUD_PROJECT  (for Google Cloud Translate)
- GOOGLE_APPLICATION_CREDENTIALS (or default ADC set up in Cloud Run)
"""

import os
import json
from typing import Any, Dict, List, Optional

from openai import OpenAI
from google.cloud import translate_v3 as translate
from google.api_core import exceptions as gexc
from google.protobuf.json_format import MessageToDict

# ---------------------------------------------------------------------------
# CONFIG / CLIENTS
# ---------------------------------------------------------------------------

# OpenAI client – OPENAI_API_KEY must be in the environment
oa_client = OpenAI()

# Project ID for Translate – prefer explicit, fall back to default project
PROJECT_ID = os.environ.get(
    "TRANSLATE_PROJECT_ID",
    os.environ.get("GOOGLE_CLOUD_PROJECT", "adsort-477810"),  # safe default
)

PARENT = f"projects/{PROJECT_ID}/locations/global"
translate_client = translate.TranslationServiceClient()

LEGACY_LANG_MAP: Dict[str, str] = {
    "iw": "he",  # Hebrew
    "ji": "yi",  # Yiddish
    "in": "id",  # Indonesian
}

# ---------------------------------------------------------------------------
# JSON SCHEMAS FOR GPT
# ---------------------------------------------------------------------------

GPT_STR_ANALYSIS_SCHEMA: Dict[str, Any] = {
    "name": "string_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "valid": {
                "type": "boolean",
                "description": "Does the string contain valid natural-language word(s) in any language?",
            },
            "dirty": {
                "type": "boolean",
                "description": "Does the string contain clutter, transliterations, semantic duplications, emojis or non-textual characters?",
            },
            "clean": {
                "type": "string",
                "description": "Return the EXACT TEXT of the orriginal string after removing ALL non-textual characters.",
            },
            "rewrite": {
                "type": "string",
                "description": "Rewrite the string in the SHORTEST POSSIBLE WAY in English, DO NOT EXCEED 160 characters!",
            },
            "places": {
                "type": "string",
                "description": "Extract a comma-separated list of ALL explicit, EXPLICIT and IMPLICIT GEOGRAPHICAL LOCATION NAMES, including ones appearing in brand names.",
            },
            "names": {
                "type": "string",
                "description": "Extract a comma-separated list of ALL ENTITY-NAMES and PERSON-NAMES.",
            },
            "topics": {
                "type": "string",
                "description": "Extract ABSTRACT NOUNS or ADJECTIVES indicating on the topical focus of the channel. CONVERT them to non-promissory, non-sensational, non-hype and non-clickbate words! Return the result in a comma-separated list.",
            },
            "keywords": {
                "type": "string",
                "description": "Reply a comma-separated list of up-to 3 INFERRED abstract nouns or adjectives NOT PRESENT IN THE STRING, which MAY SHED LIGHT on the CONTENT FOCUS of the channel. NO promissory, sensational, hype or clickbate words! NO PUFFERY!",
            },
            "target": {
                "type": "string",
                "description": "based ONLY on the LANGUAGE in which the string is written in and EXPLICIT INDICATIONS in the text, suggest a SINGLE country or world region the channel is targeting as AUDIENCE.",
            },
            "reason": {
                "type": "string",
                "description": "Very short explanation of the decision.",
            },
        },
        "required": [
            "valid",
            "dirty",
            "clean",
            "rewrite",
            "places",
            "names",
            "topics",
            "keywords",
            "target",
            "reason",
        ],
        "additionalProperties": False,
    },
}

GPT_TRANS_CHOICE_SCHEMA: Dict[str, Any] = {
    "name": "translation_choice",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "selection": {
                "type": "string",
                "description": "Return the text of the option representing the more accurate translation.",
            }
        },
        "required": ["selection"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# GPT HELPERS
# ---------------------------------------------------------------------------


def gpt_analysis(string: str) -> Dict[str, Any]:
    """
    Run GPT-based linguistic analysis on a single string.

    Returns a dict matching GPT_STR_ANALYSIS_SCHEMA.
    """
    ask_gpt = oa_client.chat.completions.create(
        model="gpt-4o-2024-08-06",
        temperature=0,
        response_format={
            "type": "json_schema",
            "json_schema": GPT_STR_ANALYSIS_SCHEMA,
        },
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert linguist, requested to analyze data "
                    "relating to a Telegram channel to help establish links "
                    "between linguistic datapoints and the orrigin, the target "
                    "audience and the topical focus of the channel. "
                    "You must respond ONLY with JSON that matches the provided schema."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Analyze the following string relating to a Telegram channel and "
                    "extract linguistic information capable of pinpointing the orrigin, "
                    "target audience and topical focus of the Telegram channel.\n\n"
                    f"String: {string}"
                ),
            },
        ],
    )

    msg = ask_gpt.choices[0].message
    raw = msg.content
    gpt_resp_dict = json.loads(raw)

    return gpt_resp_dict


def gpt_selection(options: str) -> Dict[str, Any]:
    """
    Select the better translation between two options.

    `options` is a JSON string with keys: source, option_1, option_2.
    Returns a dict matching GPT_TRANS_CHOICE_SCHEMA.
    """
    ask_gpt = oa_client.chat.completions.create(
        model="gpt-4o-2024-08-06",
        temperature=0,
        response_format={
            "type": "json_schema",
            "json_schema": GPT_TRANS_CHOICE_SCHEMA,
        },
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert linguist, requested to select between two "
                    "translation options for a source text provided in a JSON string. "
                    "You must respond ONLY with JSON that matches the provided schema."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Analyze the source text and the translation options and return the "
                    "better translation text without changing it.\n\n"
                    f"String: {options}"
                ),
            },
        ],
    )

    msg = ask_gpt.choices[0].message
    raw = msg.content
    gpt_resp_dict = json.loads(raw)

    return gpt_resp_dict


# ---------------------------------------------------------------------------
# GOOGLE TRANSLATE HELPER
# ---------------------------------------------------------------------------


def g_translate(string: str, target_language: str = "en") -> Optional[Dict[str, str]]:
    """
    Use Google Cloud Translate to:
    - detect the source language
    - translate the string into `target_language`

    Returns:
        {
            "lang": "<detected_lang>",
            "trans": "<translated_text>",
        }
    or None on error.
    """
    try:
        response = translate_client.translate_text(
            request={
                "parent": PARENT,
                "contents": [string],
                "target_language_code": target_language,
                "mime_type": "text/plain",
            }
        )
    except gexc.GoogleAPICallError as e:
        # In production you might want to log instead of print
        print("Translate API error:", e)
        return None

    # convert google translate JSON to dict
    response_dict = MessageToDict(response._pb, preserving_proto_field_name=True)

    translations = (response_dict.get("translations") or [])
    if not translations:
        return None

    translations = translations[0]

    result = {
        "lang": translations.get("detected_language_code"),
        "trans": translations.get("translated_text"),
    }

    # convert legacy language codes
    if result["lang"] in LEGACY_LANG_MAP:
        result["lang"] = LEGACY_LANG_MAP[result["lang"]]

    return result


# ---------------------------------------------------------------------------
# HIGH-LEVEL STRING ANALYSIS PIPELINE
# ---------------------------------------------------------------------------


def str_analysis(string: str, target_language: str = "en") -> Dict[str, Any]:
    """
    High-level pipeline:
    1. GPT linguistic analysis on `string`.
    2. Prefer GPT's `clean` version if available.
    3. Merge GPT `places`, `names`, `topics`, `keywords` into a deduped list.
    4. If GPT says the string is valid, run Google Translate.
    5. Ask GPT to select the better English translation between Google & GPT rewrite.
    6. Return a compact, Firestore-ready result dict.

    Returns:
        {
            "src": <final_str>,
            "lang": <detected_lang or None>,
            "eng": <best_english_under_160_chars>,
            "target": <gpt_target>,
            "keywords": [<merged_keyword_list>] or None,
        }
    """
    # 1) run GPT analysis
    gpt = gpt_analysis(string)

    # 2) prefer GPT-clean text when available
    clean_text = gpt.get("clean")
    final_str = clean_text if clean_text else string

    # 3) merge "places", "names", "topics" and "keywords" into a deduped list
    keyword_sources = [
        gpt.get("places"),
        gpt.get("names"),
        gpt.get("topics"),
        gpt.get("keywords"),
    ]

    keywords_flat: List[str] = []
    for source in keyword_sources:
        if not source:
            continue
        if isinstance(source, str):
            keywords_flat.append(source)
        elif isinstance(source, list):
            keywords_flat.extend(source)

    keywords: Optional[List[str]] = None
    if keywords_flat:
        raw_items = [k.strip() for k in ",".join(keywords_flat).split(",") if k.strip()]
        seen = set()
        deduped: List[str] = []
        for item in raw_items:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        keywords = deduped or None

    # 4) Google Translation
    gtrans: Optional[Dict[str, str]] = None
    if gpt.get("valid"):
        gtrans = g_translate(final_str, target_language=target_language)

    # 5) select best translation
    best: Optional[str] = None
    if not gtrans:
        best = gpt.get("rewrite")
    else:
        trans_options_dict = {
            "source": final_str,
            "option_1": gtrans.get("trans") if gtrans else None,
            "option_2": gpt.get("rewrite"),
        }
        trans_options = json.dumps(trans_options_dict, indent=2, ensure_ascii=False)
        best = gpt_selection(trans_options).get("selection")

    # guardrails
    if not best:
        best = gpt.get("rewrite") or final_str

    if len(best) >= 160:
        best = gpt.get("rewrite") or best

    result = {
        "src": final_str,
        "lang": gtrans["lang"] if gtrans else None,
        "eng": best,
        "target": gpt.get("target"),
        "keywords": keywords,
    }

    return result


# ---------------------------------------------------------------------------
# OPTIONAL LOCAL TEST
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Simple manual test when running this file directly
    sample_text = "ללא צנזורה חדשות ישראל בטלגרם"
    print(json.dumps(str_analysis(sample_text, target_language="en"), indent=2, ensure_ascii=False))
