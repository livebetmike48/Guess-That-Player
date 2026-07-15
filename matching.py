"""
Guess matching: forgiving enough for a casual game (case-insensitive,
accent-insensitive, last-name-only accepted) while never rewarding a wrong
player. Same word-subset approach validated in the Statcast bot's name
matching, plus accent folding so 'Sanchez' correctly matches 'Sánchez'.
"""
import unicodedata


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().replace(".", "").replace(",", "").strip()


def guess_matches(guess: str, answer_name: str) -> bool:
    """Every word in the guess must appear in the answer name, AND the
    guess must include the answer's last name -- so 'sullivan' and 'sean
    sullivan' match 'Sean Sullivan', but a lone first name ('sean', 'juan')
    is not enough to win."""
    guess_words = set(_normalize(guess).split())
    answer_words = _normalize(answer_name).split()
    if not guess_words or not answer_words:
        return False
    last_name = answer_words[-1]
    return guess_words.issubset(set(answer_words)) and last_name in guess_words
