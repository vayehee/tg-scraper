# gtranslate.py

import os
import logging
from typing import Optional, Tuple, Dict

try:
    from google.cloud import translate_v3 as translate
    TRANSLATE_CLIENT = translate.TranslationServiceClient()
except ImportError:  # v3 not available, fallback to v2
    from google.cloud import translate_v2 as translate
    TRANSLATE_CLIENT = translate.Client()

logger = logging.getLogger("tg-scraper.gtranslate")

# ---------------------------
# Config
# ---------------------------
PROJECT_ID = os.getenv("PROJECT_ID")
TRANSLATE_LOCATION = os.getenv("TRANSLATE_LOCATION", "global")


# ---------------------------
# Utilities
# ---------------------------
# Legacy language code mappings
LEGACY_LANG_MAP = {
    "iw": "he",  # Hebrew
    "ji": "yi",  # Yiddish
    "in": "id",  # Indonesian
}


# ---------------------------
# Methods
# ---------------------------

# Detect language
def DETECT(text: str) -> Tuple[Optional[str], float]:
    """Returns ("language_code", "certitude") tuple. Safe on errors."""
    
    # 1. clean the text str input
    text = (text or "").strip()

    # 2. return None on empty text str input
    if not text:
        return None, 0.0
    
    # 3. Ensure GCP project to use
    project = PROJECT_ID
    if not project:
        logger.warning("Google Cloud PROJECT_ID not set!")
        return None, 0.0
    
    # 4. Call Google Cloud Translate API "detect_language" endpoint
    try:
        client = TRANSLATE_CLIENT
        parent = f"projects/{project}/locations/{TRANSLATE_LOCATION}"
        resp = client.detect_language(
            request={
                "parent": parent,
                "content": text,
                "mime_type": "text/plain",
            }
        )

        # 4.1. No language detected? Return None!
        if not resp.languages:
            return None, 0.0
        
        # 4.2. Language(s) detected? pick the best language by "certitude" response
        best = max(resp.languages, key=lambda l: getattr(l, "confidence", 0.0))

        # 4.3. Normalize legacy language codes
        code = (best.language_code or "").lower()
        code = LEGACY_LANG_MAP.get(code, code)

        # 4.4. Return the best detected language code and its certitude
        return code, float(getattr(best, "confidence", 0.0))
    
    # 5. Handle errors & exceptions gracefully
    except Exception as e:  # keep service resilient
        logger.warning("gtranslate detect_language failed: %s", e)
        return None, 0.0