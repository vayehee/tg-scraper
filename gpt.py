# gpt.py

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from openai import OpenAI
client = OpenAI()

import logging
logger = logging.getLogger("tg-scraper.gpt")


# ---------------------------
# Config
# ---------------------------
OPENAI_MODEL = {
    "4o": "gpt-4o",        # flagship omni model (general-purpose, multimodal)
    "4om": "gpt-4o-mini",  # fast, cheap omni variant for high-volume tasks
    "4o1": "gpt-4.1",      # strong GPT-4 family model for general use
    "4o1m": "gpt-4.1-mini",# lighter/cheaper 4.1 variant
    "5o1": "gpt-5.1",      # latest high-intelligence model for complex tasks
    "5o1m": "gpt-5.1-mini",# smaller 5.1, good balance of cost and quality
    "o3": "o3",            # strongest reasoning model for deep step-by-step tasks
    "o4m": "o4-mini",      # lightweight reasoning model, cheaper & faster than o3
}


# ---------------------------
# Utilities
# ---------------------------
CATEGORIES = [
    "News",
    "Politics",
    "Business",
    "Finance",
    "Tech",
    "Cybersecurity",
    "Lifestyle",
    "Sports",
    "Education",
    "OSINT",
    "NSFW",
    "Porn",
    "Memes",
    "Deals",
    "Gaming",
    "Health",
    "Culture",
    "Gore",
    "Unknown",
]


# ---------------------------
# Methods
# ---------------------------

# ask ChatGPT to analyze a Telegram channel JSON and extract insights
def CHANANALYSE(chan_json: str) -> Dict[str, Any]:

    resp_schema = {
        "name": "text_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "name_en": {
                    "type": "string",
                    "description": (
                        'Rewrite the channel name from "chan_name" into clear, natural English. '
                        "Preserve the meaning, avoid added interpretation, and keep it concise."
                    ),
                },
                "desc_en": {
                    "type": "string",
                    "description": (
                        'Rewrite "chan_description" in English as a single short sentence. '
                        "MUST NOT exceed 90 characters (including spaces). No hashtags or emojis."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Analyze ALL fields in the user-provided JSON and return ONE channel category. "
                        f"The value MUST be exactly one of the following options: {CATEGORIES}. "
                        "Choose the single best match only."
                    ),
                },
                "locations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Return an array of strings. Each element is the name of a GEOGRAPHICAL LOCATION "
                        "(cities, regions, countries, places) explicitly or implicitly mentioned anywhere "
                        "in the JSON. Use English names where possible. No duplicates, no explanations."
                    ),
                },
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Return an array of strings. Each element is a PERSON name or ENTITY name "
                        "(people, organizations, companies, parties, media outlets, groups, etc.) "
                        "appearing anywhere in the JSON. No duplicates, no extra commentary."
                    ),
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Based on all values in the JSON, compile an array of strings. Each element is an ABSTRACT NOUN or ADJECTIVE which "
                        "describes the channel's topical focus (e.g. \"politics\", \"finance\", \"satirical\", \"propaganda\"). "
                        "OMIT promissory, sensational, hype or clickbait terms."
                    ),
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Return an array of up to 3 strings. Each element is an INFERRED abstract noun or "
                        "adjective that does NOT appear in the JSON text but logically follows from the "
                        "content and focus of the channel. NO promissory, sensational, hype or clickbait words."
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Based ONLY on EXPLICIT clues and the LANGUAGE of the JSON fields, return exactly ONE "
                        "country name in English that the channel most likely targets as its audience (e.g. 'France', 'Turkey'). "
                        'If JSON key values are in English and no EXPLICIT clues on target audience present, choose "International". '
                        'If JSON key values are NOT in English and no EXPLICIT clues on target audience present, choose the single most likely country.'
                    ),
                },
            },
            "required": [
                "name_en",
                "desc_en",
                "category",
                "locations",
                "names",
                "topics",
                "keywords",
                "target",
            ],
            "additionalProperties": False,
        },
    }

    ask_gpt = client.chat.completions.create(
        model=OPENAI_MODEL["4o"],
        temperature=0,
        response_format={
            "type": "json_schema",
            "json_schema": resp_schema,
        },
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert Telegram channels inspector. "
                    "Following is a JSON object containing scraped info from a specific Telegram channel, "
                    "in the following schema:\n"
                    "{\n"
                    '   "chan_username": "<string>",\n'
                    '   "chan_name": "<string>",\n'
                    '   "chan_description": "<string>",\n'
                    "}\n"
                    "Analyze the user-provided JSON containing info on a specific Telegram channel and "
                    "extract meaningful insights. You must respond ONLY with JSON that matches the provided schema."
                ),
            },
            {
                "role": "user",
                "content": f"Telegram Channel Info JSON:\n\n{chan_json}",
            },
        ],
    )

    raw = ask_gpt.choices[0].message.content

    try:
        resp = json.loads(raw or "{}")
    except json.JSONDecodeError:
        logger.warning("Failed to parse CHANANALYSE JSON: %r", raw)
        resp = {"error": f"Failed to parse CHANANALYSE JSON: {raw!r}"}

    return resp


# ask ChatGPT to rewrite a string to a given length in a given language
def REWRITE(string: str, length: int, lang: Optional[str] = None) -> Dict[str, Any]:

    # if output "lang" is None, default to English
    if lang is None:
        lang = "English"

    # define response schema (prompt)
    resp_schema = {
        "name": "text_rewrite",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "rewrite": {
                    "type": "string",
                    "description": (
                        f"Rewrite the user-provided text in {lang}. Maximum length: {length} characters. "
                        "If rewrite needs multiple paragraphs, use short paragraphs separated by empty lines."
                    ),
                },
            },
            "required": ["rewrite"],
            "additionalProperties": False,
        },
    }
    
    # ask GPT to process the prompt and reply according to schema
    ask_gpt = client.chat.completions.create(
        model=OPENAI_MODEL["4om"],  # gpt-4o-mini is safest for json_schema in chat.completions
        temperature=0,
        response_format={
            "type": "json_schema",
            "json_schema": resp_schema,
        },
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert writer. "
                    f"Rewrite user-provided text in {lang}, with a maximum of {length} characters. "
                    "Use short paragraphs separated by empty lines. "
                    "You must respond ONLY with JSON that matches the provided schema."
                ),
            },
            {
                "role": "user",
                "content": f"Text to rewrite:\n\n{string}",
            },
        ],
    )

    raw = ask_gpt.choices[0].message.content

    try:
        resp = json.loads(raw or "{}")
    except json.JSONDecodeError:
        # Optionally log the raw output for debugging
        logger.warning("Failed to parse REWRITE JSON: %r", raw)
        resp = {"rewrite": f"Failed to parse REWRITE JSON: {raw!r}"}

    return resp