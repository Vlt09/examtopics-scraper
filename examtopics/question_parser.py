
import re
from html import unescape
from typing import Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .settings import BASE_URL


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _image_urls(element, page_url: str) -> List[str]:
    """Return the absolute URLs of every <img> inside `element`, in order, deduplicated."""
    if element is None:
        return []
    base = page_url or BASE_URL
    urls: List[str] = []
    for img in element.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
        src = unescape(src).strip()
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(base, src)
        if absolute not in urls:
            urls.append(absolute)
    return urls


def parse_question_page(html: str, url: str = "") -> Dict:
    """
    Returns a dict:
    {
        "url": str,
        "question_no": str,          # e.g. "Topic 1 / Question 23"
        "question": str,             # question text only, options stripped out
        "options": {"A": "...", "B": "...", ...},
        "images": [str, ...],        # absolute URLs of images in the question body
        "answer_images": [str, ...], # absolute URLs of images in the suggested answer
        "most_voted": str,           # e.g. "B"
        "vote_counts": {"A": 5, "B": 12, ...},
        "community_answer": str,     # most_voted letter + text
        "discussion": [str, ...],    # list of comment strings
    }
    """
    soup = BeautifulSoup(html, "html.parser")
    result: Dict = {"url": url}

    # ── Question number ──────────────────────────────────────────────
    q_label = soup.select_one(".question-title-topic-details, .question-number, h1")
    result["question_no"] = _clean(q_label.get_text()) if q_label else ""

    # ── Question body ─────────────────────────────────────────────────
    # ExamTopics puts the question text in .question-body or .card-text
    q_body = (
        soup.select_one(".question-body")
        or soup.select_one(".card-text p")
        or soup.select_one(".question-text")
    )
    if q_body:
        # Work on a copy so we don't disturb the original soup (options are
        # parsed from `soup` separately below and must stay intact there).
        # This is the fix for the "question field contains options" issue:
        # strip out option/answer elements BEFORE extracting question text.
        q_body_copy = BeautifulSoup(str(q_body), "html.parser")
        for junk in q_body_copy.select(
            "li.multi-choice-item, ul, ol, .multi-choice-letter, "
            ".answer, .options, .vote-button, .answer-vote-button"
        ):
            junk.decompose()
        # Collect question images (exhibits, diagrams, hotspot/drag-drop
        # screenshots) without the ones belonging to the suggested answer.
        images_copy = BeautifulSoup(str(q_body_copy), "html.parser")
        for answer_el in images_copy.select(".question-answer, .correct-answer, .answer-description"):
            answer_el.decompose()
        result["images"] = _image_urls(images_copy, url)
        question_text = _clean(q_body_copy.get_text(" "))
        # Strip trailing site boilerplate button text that leaks in
        # regardless of which element it's actually wrapped in.
        question_text = re.sub(r"\s*Show Suggested Answer\s*$", "", question_text, flags=re.I).strip()
        result["question"] = question_text
    else:
        result["question"] = ""
        result["images"] = []

    # ── Suggested-answer images (hotspot / drag-and-drop answers) ─────
    answer_images: List[str] = []
    for answer_el in soup.select(".correct-answer, .answer-description"):
        for img_url in _image_urls(answer_el, url):
            if img_url not in answer_images:
                answer_images.append(img_url)
    result["answer_images"] = answer_images

    # ── Options ───────────────────────────────────────────────────────
    options: Dict[str, str] = {}
    # Strategy 1: li.multi-choice-item
    for li in soup.select("li.multi-choice-item"):
        letter_el = li.select_one(".multi-choice-letter, strong")
        if letter_el:
            letter = _clean(letter_el.get_text()).strip(".")
            letter_el.decompose()
            text = _clean(li.get_text(" "))
            for img_url in _image_urls(li, url):
                text = f"{text} [Image: {img_url}]".strip()
            options[letter] = text
    # Strategy 2: fallback — lines starting with A. B. C. D. inside question card
    if not options:
        card = soup.select_one(".question-card, .card")
        if card:
            raw = card.get_text("\n")
            for line in raw.splitlines():
                m = re.match(r"^\s*([A-F])\s*[.)]\s*(.+)", line)
                if m:
                    options[m.group(1)] = _clean(m.group(2))
    result["options"] = options

    # ── Votes ─────────────────────────────────────────────────────────
    vote_counts: Dict[str, int] = {}
    most_voted = ""

    # ExamTopics vote buttons: <span class="vote-letter">B</span> <span class="vote-count">12</span>
    for btn in soup.select(".vote-button, .answer-vote-button"):
        letter_el = btn.select_one(".vote-letter, .answer-letter")
        count_el  = btn.select_one(".vote-count, .answer-votes")
        if letter_el:
            letter = _clean(letter_el.get_text())
            count  = int(re.sub(r"\D", "", count_el.get_text() or "0") or 0) if count_el else 0
            vote_counts[letter] = count

    # Most voted answer badge
    badge = soup.select_one(".most-voted-answer, .community-vote-result")
    if badge:
        m = re.search(r"([A-F])", badge.get_text())
        if m:
            most_voted = m.group(1)

    # Fallback: derive from vote_counts
    if not most_voted and vote_counts:
        most_voted = max(vote_counts, key=lambda k: vote_counts[k])

    # ── Discussion comments ───────────────────────────────────────────
    comments: List[str] = []
    for comment in soup.select(".comment-body, .discussion-comment, .card-comment"):
        text = _clean(comment.get_text(" "))
        if text and len(text) > 10:
            comments.append(text)
    result["discussion"] = comments[:10]

    # ── Derive most_voted from "Selected Answer: X" in discussion ────
    if not most_voted and comments:
        tally: Dict[str, int] = {}
        for c in comments:
            m = re.search(r"Selected Answer:\s*([A-F]+)", c, re.I)
            if m:
                for letter in m.group(1).upper():
                    tally[letter] = tally.get(letter, 0) + 1
        if tally:
            most_voted = max(tally, key=lambda k: tally[k])
            vote_counts = tally  # use tally as proxy vote counts

    result["vote_counts"] = vote_counts
    result["most_voted"] = most_voted
    if most_voted and most_voted in options:
        result["community_answer"] = f"{most_voted}. {options[most_voted]}"
    elif most_voted:
        result["community_answer"] = most_voted
    else:
        result["community_answer"] = "Not available"

    return result
