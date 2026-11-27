"""Fixed replies for messages that match no banking topic.

The text is templated: nothing the model wrote and nothing from the customer's
message is shown. A pure greeting, thanks or goodbye gets a short friendly
reply; every other no-match message gets the reply that lists a few topics.
"""

import re

TOPICS = (
    "a card that has not arrived",
    "a lost or stolen card",
    "a transfer the recipient has not received",
    "exchange rates",
    "a failed top-up",
)

_TOPIC_LIST = "; ".join(TOPICS)
_DEMO = "This is a fictional bank demo."

UNKNOWN_REPLY = (
    "I'm sorry, I could not match that message to a demo banking topic. "
    f"I can help with things like {_TOPIC_LIST}. "
    "Could you describe the banking problem you need help with? "
    "Please rephrase your request with the details. "
    f"{_DEMO}"
)

GREETING_REPLY = (
    "Hello! I'm a banking support demo. "
    f"I can help with things like {_TOPIC_LIST}. "
    f"What do you need help with? {_DEMO}"
)

THANKS_REPLY = (
    f"You're welcome! If you have another banking question, just ask. {_DEMO}"
)

GOODBYE_REPLY = f"Goodbye! Come back any time you have a banking question. {_DEMO}"

_GREETING = re.compile(
    r"(hi|hello|hey|hiya|howdy|yo|good (morning|afternoon|evening)|greetings)"
    r"( (there|team|support|bot))?"
)
_THANKS = re.compile(
    r"(thanks|thank you|thankyou|thx|ty|cheers|much appreciated"
    r"|thanks a lot|thank you (so|very) much|thanks (so|very) much)"
    r"( (a lot|so much|very much|again|for your help))?"
)
_GOODBYE = re.compile(
    r"(bye|goodbye|good bye|see you|see ya|cya|good night|take care|bye bye)"
    r"( (now|then|for now|later|soon))?"
)


def no_match_reply(message: str) -> str:
    """The fixed reply for a message that matched no banking topic."""
    # Lowercase words only; punctuation and emoji are dropped. A message with
    # anything else in it (a greeting plus a question) is not a pure greeting.
    text = " ".join(re.findall(r"[a-z]+", message.lower()))
    if _GREETING.fullmatch(text):
        return GREETING_REPLY
    if _THANKS.fullmatch(text):
        return THANKS_REPLY
    if _GOODBYE.fullmatch(text):
        return GOODBYE_REPLY
    return UNKNOWN_REPLY
