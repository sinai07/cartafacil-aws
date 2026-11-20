"""
letter-explainer-fn
Explains an official letter in plain language using Amazon Bedrock (Nova 2 Lite).

Input (console test event or API Gateway body):
  {"code": "ANA-7K2P", "letter_text": "..."}                               -> text of the letter
  {"code": "ANA-7K2P", "image_base64": "...", "image_format": "jpeg|png"}  -> photo of the letter

Every friend gets a code with a fixed number of uses (stored in DynamoDB).
Each letter spends one use. When uses_left reaches 0, the code stops working.

Environment variables (optional):
  MODEL_ID         default: us.amazon.nova-2-lite-v1:0
  OUTPUT_LANGUAGE  default: Spanish
  CODES_TABLE      default: letter-explainer-codes
"""

import base64
import json
import os
import re
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

bedrock = boto3.client("bedrock-runtime")
dynamodb = boto3.client("dynamodb")
CODES_TABLE = os.environ.get("CODES_TABLE", "letter-explainer-codes")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
OUTPUT_LANGUAGE = os.environ.get("OUTPUT_LANGUAGE", "Spanish")

# Fixed marker for "this is not in the letter". It never changes with the language,
# so the web page can always hide those fields.
NOT_FOUND = "NOT_IN_LETTER"

SYSTEM_PROMPT = f"""You help people who struggle with official letters (school, insurance,
hospital, bank, city, utilities, work, immigration) understand what the letter says and
what they must do. The person should NOT need to leave the app or search the web:
everything they need to comply must be in your answer.

Rules:
- Write every explanation in {OUTPUT_LANGUAGE}, in plain, simple words, as if explaining
  to a family member.
- Never invent anything. If a detail is not in the letter, write exactly: {NOT_FOUND}
- Copy times, amounts, addresses, phone numbers, names, and account or case numbers
  exactly as they appear. Keep times in the letter's format (e.g. "3:30 PM", never "15:30").
- DATES: ALWAYS write dates in {OUTPUT_LANGUAGE}, with the original from the letter in
  parentheses, e.g. "24 de noviembre de 2026 (November 24, 2026)". Do NOT add the
  weekday yourself; the code adds it.
- MONEY: if the letter asks for a payment, put the exact amount in "amount_due".
- NUMBERS: put every account, case, invoice, student or reference number in
  "reference_numbers". The person will need them to pay, call or show up.
- LINKS: include ONLY web addresses that are written in the letter, copied character by
  character. NEVER invent, complete or guess a link, even if you know the official site.
  If the letter has no link, leave the list empty and explain in "how_to_comply" the other
  way to do it (phone, in person, mail).
- HELP: look carefully for any help the letter offers (free interpreter or translation,
  payment plan, financial assistance, deadline extension, accommodations) and put it in
  "help_available". For people who don't read the letter's language, this is one of the
  most important parts.
- If the letter names a contact person, put them in "contact". If it only names an
  office or department, put that instead.
- KEY TERMS: "term" is the exact English word or phrase as written in the letter (do NOT
  translate it), and "meaning" explains it in {OUTPUT_LANGUAGE}. This helps the person
  recognize those words in future letters.
- Do not give legal, medical or financial advice. If the letter is about legal,
  immigration or debt matters, recommend talking to a professional.
- If the input is not a letter or cannot be read, say so in "summary" and set the
  other fields to {NOT_FOUND}.

Return ONLY a valid JSON object, with no text before or after it, using these keys:
{{
  "letter_type": "bill, notice, appointment, summons, collection, approval, denial, etc.",
  "sender": "name of the organization",
  "summary": "2 or 3 sentences explaining what the letter is about",
  "decision": "what was decided or what will happen (e.g. you were approved, your service will be cut off if you don't pay)",
  "date": "appointment date or deadline",
  "time": "exact time, in the letter's format",
  "amount_due": "exact amount to pay, or NOT_IN_LETTER",
  "reference_numbers": [{{"type": "account, case, invoice, student ID, etc.", "number": "copied exactly"}}],
  "place": "address or place if you must go in person",
  "contact": "person to ask for, with their role",
  "what_to_do": ["action 1", "action 2"],
  "requirements": ["document or item to bring or send"],
  "how_to_comply": "step by step: online, by phone, in person",
  "links": [{{"purpose": "pay, reschedule, fill out form, etc.", "url": "link copied from the letter"}}],
  "phone_numbers": [{{"purpose": "customer service, reschedule, etc.", "number": "copied from the letter"}}],
  "help_available": ["free help the letter offers (interpreter, payment plan, etc.)"],
  "if_you_dont_comply": "consequence stated in the letter",
  "urgency": "high, medium or low",
  "key_terms": [{{"term": "exact English word from the letter", "meaning": "plain meaning in {OUTPUT_LANGUAGE}"}}]
}}"""

_NOT_FOUND_VARIANTS = (
    "not_in_letter", "not in the letter", "not specified", "not mentioned", "not stated",
    "no aparece", "no se especifica", "no se menciona", "no se indica",
)


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def _parse_json(text):
    """Nova sometimes wraps JSON in ```json fences; strip them before parsing."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(cleaned)


def _normalize_missing(value):
    """The model doesn't always write the exact marker ("Not specified", "Not mentioned"...).
    Turn every variant into NOT_FOUND so the web page can rely on one value."""
    if isinstance(value, str) and value.strip().lower().startswith(_NOT_FOUND_VARIANTS):
        return NOT_FOUND
    if isinstance(value, list):
        return [_normalize_missing(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_missing(v) for k, v in value.items()}
    return value


_SPANISH_WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_DATE_FORMATS = ("%B %d, %Y", "%A, %B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d")


def _fix_weekday(date_text):
    """LLMs are bad at calendar math (in testing, Nova called Tuesday Nov 24, 2026
    a Friday). So the code computes the weekday from the original date in
    parentheses and writes it itself."""
    if not isinstance(date_text, str) or OUTPUT_LANGUAGE.lower() != "spanish":
        return date_text
    match = re.search(r"\(([^)]+)\)\s*$", date_text)
    if not match:
        return date_text
    original = match.group(1).strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(original, fmt)
            break
        except ValueError:
            continue
    else:
        return date_text  # unknown format: leave it as the model wrote it
    weekday = _SPANISH_WEEKDAYS[parsed.weekday()]
    # Remove any weekday the model added, then put the correct one.
    rest = re.sub(r"^\s*(" + "|".join(_SPANISH_WEEKDAYS) + r"),?\s+", "", date_text, flags=re.I)
    return f"{weekday} {rest}"


def _check_links(explanation, letter_text):
    """Safety net: a made-up link could send someone to a scam page.
    With text input, drop any URL that is not literally in the letter.
    With a photo we can't compare, so we flag it for the person to double-check."""
    links = explanation.get("links") or []
    if letter_text:
        source = letter_text.lower()
        kept = [l for l in links if str(l.get("url", "")).lower().strip() in source]
        if len(kept) < len(links):
            print(json.dumps({"links_removed": len(links) - len(kept)}))
        explanation["links"] = kept
    elif links:
        explanation["links_warning"] = (
            "Compare each link with the one printed on the letter before opening it. "
            "Never enter bank details if the link doesn't match.")
    return explanation


def _spend_one_use(code):
    """Atomically take one use from the code. DynamoDB only does it if the code exists,
    is active and still has uses left, so two letters sent at the same second can never
    use the same last use twice. Returns the uses left after this one, or None."""
    try:
        result = dynamodb.update_item(
            TableName=CODES_TABLE,
            Key={"code": {"S": code}},
            UpdateExpression="SET uses_left = uses_left - :one",
            ConditionExpression="attribute_exists(code) AND active = :yes AND uses_left > :zero",
            ExpressionAttributeValues={":one": {"N": "1"}, ":zero": {"N": "0"}, ":yes": {"BOOL": True}},
            ReturnValues="UPDATED_NEW",
        )
        return int(result["Attributes"]["uses_left"]["N"])
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


def _refund_use(code):
    """If Bedrock fails after we spent a use, give it back: the friend got nothing."""
    dynamodb.update_item(
        TableName=CODES_TABLE,
        Key={"code": {"S": code}},
        UpdateExpression="SET uses_left = uses_left + :one",
        ExpressionAttributeValues={":one": {"N": "1"}},
    )


def lambda_handler(event, context):
    # Works both from the console (plain event) and from API Gateway (event["body"]).
    body = event
    if isinstance(event.get("body"), str):
        body = json.loads(event["body"])

    code = str(body.get("code", "")).strip().upper()
    letter_text = body.get("letter_text")
    image_b64 = body.get("image_base64")

    if image_b64:
        content = [
            {
                "image": {
                    "format": body.get("image_format", "jpeg"),
                    "source": {"bytes": base64.b64decode(image_b64)},
                }
            },
            {"text": "Explain this letter."},
        ]
    elif letter_text:
        content = [{"text": f"Explain this letter:\n\n{letter_text}"}]
    else:
        return _response(400, {"error": "Send letter_text or image_base64."})

    # Check the code BEFORE calling the AI: an invalid code costs nothing.
    if not code:
        return _response(400, {"error": "Missing code."})
    uses_left = _spend_one_use(code)
    if uses_left is None:
        print(json.dumps({"rejected_code": code}))
        return _response(403, {"error": "Invalid code, inactive, or no uses left."})

    try:
        result = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": 1500, "temperature": 0.2},
        )
    except Exception:
        _refund_use(code)
        raise

    # Take the first text block of the answer.
    blocks = result["output"]["message"]["content"]
    answer = next(b["text"] for b in blocks if "text" in b)
    usage = result.get("usage", {})

    try:
        explanation = _check_links(_normalize_missing(_parse_json(answer)), letter_text)
        if "date" in explanation:
            explanation["date"] = _fix_weekday(explanation["date"])
    except json.JSONDecodeError:
        explanation = {"summary": answer, "warning": "The model did not return valid JSON."}

    # Log token usage so we can check the real cost per letter in CloudWatch.
    print(json.dumps({"inputTokens": usage.get("inputTokens"),
                      "outputTokens": usage.get("outputTokens")}))

    return _response(200, {"explanation": explanation, "uses_left": uses_left,
                            "token_usage": usage})
