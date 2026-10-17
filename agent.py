
import os
import re
import json
import html
import logging
import time
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import TypedDict, Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
from tavily import TavilyClient
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field


# ============================================================
# 1. CONFIGURATION
# ============================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

if not os.getenv("GOOGLE_API_KEY"):
    raise RuntimeError("GOOGLE_API_KEY is missing from .env")

if not os.getenv("TAVILY_API_KEY"):
    raise RuntimeError("TAVILY_API_KEY is missing from .env")

# llm = ChatGoogleGenerativeAI(
#     model="gemma-4-26b-a4b-it",
#     # model="gemini-3.8-flash",
#     temperature=0.2,
#     max_retries=0,
#     timeout=120,
# )

llm = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash-lite",
    temperature=0.2,
    max_output_tokens=2048,
    max_retries=0,
)

search_client = TavilyClient(
    api_key=os.environ["TAVILY_API_KEY"]
)

OUTPUT_DIR = Path("output")
DRAFT_DIR = OUTPUT_DIR / "drafts"
APPROVED_DIR = OUTPUT_DIR / "approved"

DRAFT_DIR.mkdir(parents=True, exist_ok=True)
APPROVED_DIR.mkdir(parents=True, exist_ok=True)

MAX_REVISIONS = 2
MIN_ARTICLES = 5
MAX_ARTICLES = 7


# ============================================================
# 2. STRUCTURED OUTPUT MODELS
# ============================================================

class ArticleSummary(BaseModel):
    title: str = Field(description="Accurate article headline")
    url: str = Field(description="Exact URL from the research results")
    published_date: str = Field(
        description="Publication date, or Unknown if unavailable"
    )
    summary: str = Field(
        description="Concise summary supported by the source"
    )
    key_takeaway: str = Field(
        description="Why this story matters to the reader"
    )


class ArticleSelection(BaseModel):
    articles: list[ArticleSummary] = Field(
        description="Five to seven distinct, relevant news stories"
    )


class ReviewResult(BaseModel):
    decision: Literal["PASS", "REVISE"] = Field(
        description="Whether the newsletter meets the quality criteria"
    )
    issues: list[str] = Field(
        description="Specific problems that need correction"
    )
    explanation: str = Field(
        description="Short explanation of the review decision"
    )


# ============================================================
# 3. LANGGRAPH STATE
# ============================================================

class NewsletterState(TypedDict, total=False):
    goal: str
    mode: str
    run_id: str

    plan: str
    raw_articles: list[dict]
    articles: list[dict]

    newsletter: str
    subject: str

    critique: str
    human_feedback: str 
    review_passed: bool
    revision_count: int

    status: str
    output_path: str
    metadata_path: str


# ============================================================
# 4. HELPER FUNCTIONS
# ============================================================

def notify_progress(callback, message):
    if callback:
        callback(message)


def invoke_text(prompt, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(
                "Calling AI model | attempt %s/%s",
                attempt,
                max_attempts,
            )

            response = llm.invoke(prompt)
            content = response.content

            if isinstance(content, str):
                result = content.strip()

            elif isinstance(content, list):
                text_parts = [
                    block["text"]
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and block.get("text")
                ]
                result = "\n".join(text_parts).strip()

            else:
                raise ValueError("Unexpected model response format")

            if not result:
                raise ValueError("AI model returned an empty response")

            logger.info("AI model call succeeded on attempt %s", attempt)
            return result

        except Exception:
            logger.exception(
                "AI model call failed | attempt %s/%s",
                attempt,
                max_attempts,
            )

            if attempt == max_attempts:
                logger.error("All AI model retry attempts failed")
                raise

            delay = (2 ** (attempt - 1)) + random.uniform(0, 1)
            logger.info("Retrying AI model in %.2f seconds", delay)
            time.sleep(delay)


def clean_json_text(text: str) -> str:
    """Extract JSON from a model response."""
    text = text.strip()

    # Remove Markdown code fences.
    text = re.sub(
        r"^\s*```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*```\s*$", "", text)

    # Extract the JSON object if extra text surrounds it.
    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return text.strip()

def safe_filename(value: str) -> str:
    """Make a safe filename component."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)


def valid_http_url(url: str) -> bool:
    """Accept only ordinary HTTP/HTTPS source URLs."""
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
            and not any(
                char in url for char in ("\n", "\r", "<", ">", '"')
            )
        )
    except (ValueError, TypeError):
        return False


# ============================================================
# 5. NODE 1 — PLANNING
# ============================================================

def planner(state: NewsletterState) -> dict:
    logger.info("Planning newsletter workflow")

    prompt = f"""
    You are planning a newsletter-generation task.

    User goal:
    {state['goal']}

    Current date:
    {datetime.now(timezone.utc).date().isoformat()}

    Create a concise, actionable plan covering:
    1. Understand the user's newsletter goal and target audience.
    2. Search for recent news relevant to the goal across appropriate categories.
    3. Identify credible sources and avoid duplicate stories.
    4. Select up to 7 distinct stories, ideally 5-7, or use fewer if that is all that is available.
    5. Summarize the articles using only verified supplied evidence.
    6. Generate a polished HTML newsletter.
    7. Critique the newsletter and revise it if needed.
    8. Save the output according to the requested execution mode.

    Return only the plan.
    """

    logger.info("Calling AI model: planning")
    plan = invoke_text(prompt)
    return {"plan": plan}


# ============================================================
# 6. NODE 2 — WEB RESEARCH
# ============================================================

def research(state: NewsletterState) -> dict:
    logger.info("Researching recent AI agent news")

    today = datetime.now(timezone.utc).date().isoformat()

    queries = [
        "latest major world news politics sports business technology",
        "latest breaking news entertainment science culture festivals",
        "recent important national and international news",
    ]

    collected = {}

    for query in queries:
        try:
            response = search_client.search(
                query=query,
                topic="news",
                max_results=10,
                time_range="week",
            )

            for item in response.get("results", []):
                url = (item.get("url") or "").strip()

                if not url or not valid_http_url(url):
                    continue

                # Deduplicate articles using their URLs.
                if url in collected:
                    continue

                collected[url] = {
                    "title": item.get("title", "").strip(),
                    "url": url,
                    "content": item.get("content", "").strip(),
                    "published_date": (
                        item.get("published_date") or "Unknown"
                    ),
                    "score": item.get("score", 0),
                }

        except Exception:
            logger.exception("Search failed for query: %s", query)

    articles = list(collected.values())

    raw_articles = articles[:15]

    if len(raw_articles) < MIN_ARTICLES:
        raise RuntimeError(
            f"Only {len(articles)} usable articles were found. "
            "Try broader queries or check the Tavily API."
        )

    logger.info("Collected %d unique search results", len(raw_articles))

    return {"raw_articles": raw_articles}


# ============================================================
# 7. NODE 3 — ARTICLE SELECTION AND SUMMARIZATION
# ============================================================

def select_articles(state: NewsletterState) -> dict:
    logger.info("Selecting and summarizing articles")

    raw_articles = state["raw_articles"]

    compact_material = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "published_date": item.get("published_date") or "Unknown",
            "content": (
                item.get("content")
                or item.get("raw_content")
                or item.get("summary")
                or ""
            )[:500],
        }
        for item in raw_articles[:15]
    ]

    prompt = f"""You are an evidence-focused editor for a general news newsletter.

Goal: {state['goal']}

Select up to {MAX_ARTICLES} distinct stories, ideally {MIN_ARTICLES}-{MAX_ARTICLES}.

Rules:
- Cover a broad range of news categories, including politics, sports,
  festivals, technology, business, entertainment, science, health,
  culture, and international events, when relevant to the user's goal.
- Do not prioritize any category unless the user's goal requests it.
- Prioritize stories based on recency, significance, credibility, and relevance.
- Include diverse topics when the available evidence supports them.
- Choose credible sources and avoid duplicate events.
- Use only supplied evidence; never invent facts, quotes, or dates.
- Preserve exact source URLs.
- Use "Unknown" for unavailable publication dates.
- Write summaries in exactly 2 concise sentences.
- Include one short takeaway explaining why each story matters.
- Select fewer than 5 stories if fewer than 5 valid stories are available.

Research:
{json.dumps(compact_material, ensure_ascii=False)}

Return ONLY valid JSON:
{{
  "articles": [
    {{
      "title": "Article title",
      "url": "Exact supplied URL",
      "published_date": "YYYY-MM-DD or Unknown",
      "summary": "Evidence-based summary",
      "key_takeaway": "Why this matters"
    }}
  ]
}}

No Markdown fences or extra commentary.
"""

    logger.info(
        "Sending %d compact articles to AI model",
        len(compact_material),
    )

    logger.info("Calling AI model: article selection")
    try:
        response_text = invoke_text(prompt)
    except Exception:
        logger.exception("AI model failed during article selection")
        raise
    logger.info("AI model article-selection request completed")

    try:
        selection_data = json.loads(clean_json_text(response_text))
        selection = ArticleSelection(**selection_data)
    except Exception as exc:
        logger.exception("Failed to parse AI model article-selection JSON")
        raise ValueError(
            f"AI model returned invalid article-selection JSON: {exc}"
        ) from exc

    # Allow only URLs actually returned by the search tool.
    source_by_url = {article["url"]: article for article in raw_articles}

    selected = []
    used_urls = set()

    for article in selection.articles:
        url = article.url.strip()

        if url not in source_by_url or url in used_urls:
            continue

        if not valid_http_url(url):
            continue

        source = source_by_url[url]

        selected.append(
            {
                "title": source["title"] or article.title,
                "url": url,
                "published_date": (
                    article.published_date
                    if article.published_date != "Unknown"
                    else source["published_date"]
                ),
                "summary": article.summary,
                "key_takeaway": article.key_takeaway,
            }
        )

        used_urls.add(url)

        if len(selected) == MAX_ARTICLES:
            break

    if len(selected) == 0:
        raise RuntimeError(
            "No valid articles were selected. Please try running again."
        )

    if len(selected) < MIN_ARTICLES:
        logger.warning(
            "Only %d valid articles were selected. Continuing with available articles.",
            len(selected),
        )

    logger.info("Selected %d articles", len(selected))

    return {"articles": selected}


# ============================================================
# 8. NODE 4 — NEWSLETTER GENERATION
# ============================================================

def write_newsletter(state: NewsletterState) -> dict:
    logger.info("Generating HTML newsletter")

    articles = state["articles"]

    feedback = state.get("critique", "No previous review.")
    human_feedback = state.get("human_feedback", "").strip()
    revision_count = state.get("revision_count", 0)

    prompt = f"""
    You are a professional newsletter editor.

    User goal:
    {state['goal']}

    Newsletter plan:
    {state['plan']}

    Revision number: {revision_count}

    Previous reviewer feedback:
    {feedback}

    Human feedback from the newsletter owner:
    {human_feedback or "No additional human feedback provided."}

    Important: If human feedback is provided, prioritize it when revising
    the newsletter. Preserve the verified article URLs and do not invent
    facts. Apply the requested changes while retaining the required
    newsletter structure.

    Verified article summaries:
    {json.dumps(articles, ensure_ascii=False)}

    Create a polished newsletter in HTML.

    Return exactly this structure:
    SUBJECT: A concise, engaging email subject
    HTML:
    followed by the complete HTML document.

    Newsletter requirements:
    - Include all selected stories, ideally 5-7.
    - Include a clear title and a short introduction.
    - Give each story a headline, summary, key takeaway,
      publication date when known, and source link.
    - End with a short closing note.
    - Use clean, email-friendly HTML and inline CSS.
    - Keep summaries easy to scan.
    - Use only the supplied verified source URLs.
    - Do not introduce unsupported factual claims.
    - Do not include JavaScript, forms, tracking pixels,
      external scripts or embedded iframes.
    - Do not claim the newsletter has been emailed.
    - Incorporate the previous review feedback.
    - Cover the selected stories without forcing them into a single topic.
    - Use a headline and introduction suitable for a general news audience.
    - Include all selected stories, even if fewer than 5 are available.
    - Do not add stories that are not present in the verified article summaries.

    Return no commentary outside the requested format.

    STRICT LINK RULES:
    - Use only the exact article URLs provided below.
    - Copy URLs exactly as provided; do not shorten, modify, or invent URLs.
    - Do not add links to homepages or other external websites.
    - Every article link must point to one of the provided article URLs.
    """

    logger.info("Calling AI model: newsletter writing")
    response = invoke_text(prompt)

    subject_match = re.search(
        r"SUBJECT:\s*(.*?)\s*(?:\r?\n)+HTML:\s*",
        response,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not subject_match:
        raise ValueError(
            "The writer did not return the expected SUBJECT/HTML format."
        )

    subject = subject_match.group(1).strip()
    newsletter_html = response[subject_match.end():].strip()

    if newsletter_html.startswith("```"):
        newsletter_html = clean_json_text(newsletter_html)

    if not re.search(
        r"<html[\s>]",
        newsletter_html,
        flags=re.IGNORECASE,
    ):
        raise ValueError("The generated newsletter is not a full HTML document.")

    if not re.search(
        r"<body[\s>]",
        newsletter_html,
        flags=re.IGNORECASE,
    ):
        raise ValueError("The generated newsletter has no HTML body.")

    # Ensure the generated newsletter links only to known sources.
    
    allowed_urls = {
        article["url"].strip()
        for article in articles
        if article.get("url")
    }

    hrefs = re.findall(
        r"""href\s*=\s*["']([^"']+)["']""",
        newsletter_html,
        flags=re.IGNORECASE,
    )

    invalid_urls = [
        url.strip()
        for url in hrefs
        if url.strip() not in allowed_urls
    ]

    if invalid_urls:
        logger.warning("Invalid newsletter URLs: %s", invalid_urls)
        raise ValueError(
            "The newsletter contains a link not present in the research."
        )

    if not hrefs:
        raise ValueError(
            "The newsletter contains no source links."
        )

    return {
        "subject": subject,
        "newsletter": newsletter_html,
        "revision_count": revision_count + 1,
    }


# ============================================================
# 9. NODE 5 — SELF-CRITIQUE
# ============================================================

def review_newsletter(state: NewsletterState) -> dict:
    logger.info("Reviewing generated newsletter")

    articles = state["articles"]
    newsletter = state["newsletter"]

    prompt = f"""
    You are a strict reviewer of a general news newsletter.

    Evaluate the newsletter against the supplied evidence.

    Check:
    1. It contains up to 7 distinct stories and uses all available valid selected stories.
    2. The stories are relevant to the user's newsletter goal and audience.
    3. The stories cover appropriate categories without unnecessary repetition.
    4. Summaries are consistent with supplied article summaries.
    5. Source links are preserved.
    6. There are no obvious invented claims or dates.
    7. The subject and newsletter are professional and readable.
    8. The HTML has a clear structure.
    9. The newsletter does not claim to have been sent.

    Article evidence:
    {json.dumps(articles, ensure_ascii=False)}

    Newsletter HTML:
    {newsletter}

    Return:
    - PASS only when all essential checks are satisfied.
    - REVISE when a material issue needs correction.
    - List specific, actionable issues.
    - Do not ask for information that is not needed.
    """

    prompt += """
    Return exactly one valid JSON object.
    Do not use Markdown code fences or add text outside the JSON.

    Required format:
    {
    "decision": "PASS",
    "issues": [],
    "explanation": "The newsletter meets the review criteria."
    }

    Rules:
    - Use double quotes for every JSON key and string.
    - Use only PASS or REVISE for decision.
    - issues must be a JSON array of strings.
    - Do not include trailing commas.
    - Ensure the JSON is complete and syntactically valid.
    """

    logger.info("Calling AI model: newsletter review")
    response_text = invoke_text(prompt)

    try:
        cleaned_response = clean_json_text(response_text)
        review_data = json.loads(cleaned_response)
        review = ReviewResult(**review_data)

    except Exception as exc:
        logger.error(
            "Review response was invalid. Raw response:\n%s",
            response_text,
        )
        logger.exception("Failed to parse Gemini review JSON")

        # Fail safely: don't mark an invalid review as passed.
        raise ValueError(
            f"Gemini returned invalid review JSON: {exc}"
        ) from exc

    # Deterministic checks supplement the LLM review.
    issues = list(review.issues)

    story_headings = len(re.findall(
        r"<h[23]\b",
        newsletter,
        flags=re.IGNORECASE,
    ))

    if len(articles) == 0 or len(articles) > MAX_ARTICLES:
        issues.append("The newsletter must contain between 1 and 7 stories.")

    if story_headings < len(articles):
        issues.append(
            "The newsletter HTML is missing one or more selected story headings."
        )

    allowed_urls = {article["url"] for article in articles}
    linked_urls = re.findall(
        r"""href\s*=\s*["'](https?://[^"']+)["']""",
        newsletter,
        flags=re.IGNORECASE,
    )

    if not linked_urls:
        issues.append("No source links were found in the HTML.")

    if any(url not in allowed_urls for url in linked_urls):
        issues.append("The HTML contains unverified source URLs.")

    passed = review.decision == "PASS" and not issues

    critique = (
        f"Decision: {'PASS' if passed else 'REVISE'}\n"
        f"Explanation: {review.explanation}\n"
        f"Issues: {json.dumps(issues, ensure_ascii=False)}"
    )

    logger.info("Review decision: %s", "PASS" if passed else "REVISE")

    return {
        "critique": critique,
        "review_passed": passed,
    }



def retry_newsletter_writing(state):
    """
    Retry newsletter writing using the articles already in memory.
    Does not repeat planning, research, or article selection.
    """

    if not state or not state.get("articles"):
        raise ValueError(
            "No selected articles are available to retry."
        )

    logger.info(
        "Retrying newsletter writing with %d saved articles",
        len(state["articles"]),
    )

    # Reuse the existing state, including selected articles.
    write_result = write_newsletter(state)

    updated_state = {
        **state,
        **write_result,
    }

    # Review the new newsletter.
    review_result = review_newsletter(updated_state)

    updated_state = {
        **updated_state,
        **review_result,
    }

    return updated_state


# ============================================================
# 10. NODE 6 — SAVE OUTPUT
# ============================================================

def save_output(state: NewsletterState) -> dict:
    """
    Save the newsletter as a draft or autonomous output.
    Human mode must not mark the draft as approved.
    """
    mode = state["mode"]
    passed = state.get("review_passed", False)

    if mode == "human":
        status = "pending_approval"
        destination = DRAFT_DIR
    elif passed:
        status = "review_passed"
        destination = OUTPUT_DIR
    else:
        status = "needs_attention"
        destination = OUTPUT_DIR

    run_id = state["run_id"]
    html_path = destination / f"newsletter_{run_id}.html"
    json_path = destination / f"newsletter_{run_id}.json"

    html_path.write_text(
        state["newsletter"],
        encoding="utf-8",
    )

    metadata = {
        "run_id": run_id,
        "goal": state["goal"],
        "mode": mode,
        "subject": state["subject"],
        "plan": state["plan"],
        "articles": state["articles"],
        "critique": state.get("critique", ""),
        "review_passed": passed,
        "revision_count": state.get("revision_count", 0),
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "human_feedback": state.get("human_feedback", ""),
    }

    json_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info("Newsletter saved to %s", html_path)

    return {
        "status": status,
        "output_path": str(html_path),
        "metadata_path": str(json_path),
    }


# ============================================================
# 11. CONDITIONAL ROUTING
# ============================================================

def route_after_review(
    state: NewsletterState,
) -> Literal["write", "save"]:
    """
    Revise failed drafts within the limit.
    After the limit, save with the appropriate status.
    """
    if state.get("review_passed", False):
        return "save"

    if state.get("revision_count", 0) <= MAX_REVISIONS:
        logger.info("Review failed; sending draft back for revision")
        return "write"

    logger.warning("Revision limit reached")
    return "save"


# ============================================================
# 12. BUILD THE LANGGRAPH WORKFLOW
# ============================================================

def build_graph(progress_callback=None):
    graph = StateGraph(NewsletterState)

    def wrap_node(name, func):
        def wrapped(state):
            if progress_callback:
                progress_callback(f"⏳ {name}...")
            result = func(state)
            if progress_callback:
                progress_callback(f"✅ {name} completed")
            return result
        return wrapped

    # Register every node before connecting edges
    graph.add_node("plan", wrap_node("Planning", planner))
    graph.add_node("research", wrap_node("Researching articles", research))
    graph.add_node("select", wrap_node("Selecting articles", select_articles))
    graph.add_node("write", wrap_node("Writing newsletter", write_newsletter))
    graph.add_node("review", wrap_node("Reviewing newsletter", review_newsletter))
    graph.add_node("save", wrap_node("Saving newsletter", save_output))

    # Entry point and workflow connections
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "research")
    graph.add_edge("research", "select")
    graph.add_edge("select", "write")
    graph.add_edge("write", "review")

    # Keep the existing review-routing behavior
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {
            "write": "write",
            "save": "save",
        },
    )

    graph.add_edge("save", END)

    return graph.compile()



# ============================================================
# 13. PUBLIC ENTRY POINT
# ============================================================

def run_newsletter_agent(
    goal: str,
    mode: str = "autonomous",
    progress_callback=None
) -> dict:
    """
    Execute the complete newsletter workflow.

    Args:
        goal: Plain-English newsletter objective.
        mode: 'autonomous' or 'human'.

    Returns:
        Dictionary containing the plan, articles, newsletter,
        review result, status and output paths.
    """
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("Please provide a non-empty newsletter goal.")

    mode = mode.strip().lower().replace("-", "_")

    if mode in {"fully_autonomous", "autonomous"}:
        mode = "autonomous"
    elif mode in {"human_in_the_loop", "human", "hitl"}:
        mode = "human"
    else:
        raise ValueError(
            "Mode must be 'autonomous' or 'human'."
        )

    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    )

    initial_state: NewsletterState = {
        "goal": goal.strip(),
        "mode": mode,
        "run_id": run_id,
        "revision_count": 0,
        "review_passed": False,
        "status": "running",
    }

    logger.info(
        "Starting newsletter agent | mode=%s | run_id=%s",
        mode,
        run_id,
    )

    # Build the graph dynamically passing the callback
    app = build_graph(progress_callback=progress_callback)
    
    # Invoke the graph with the initial state
    result = app.invoke(initial_state)

    logger.info(
        "Newsletter workflow finished | status=%s",
        result.get("status"),
    )

    return result


# ============================================================
# 14. HUMAN APPROVAL / REJECTION
# ============================================================

def approve_newsletter(result: dict) -> dict:
    """
    Approve a pending draft and save the approved newsletter.
    Call this only after the human explicitly approves it.
    """
    if result.get("mode") != "human":
        raise ValueError("This newsletter was not created in human mode.")

    if result.get("status") != "pending_approval":
        raise ValueError("The newsletter is not awaiting approval.")

    html_path = Path(result["output_path"])

    if not html_path.exists():
        raise FileNotFoundError("The draft HTML file was not found.")

    html_content = html_path.read_text(encoding="utf-8")
    run_id = result.get("run_id") or html_path.stem

    approved_path = APPROVED_DIR / f"{safe_filename(run_id)}.html"

    approved_path.write_text(
        html_content,
        encoding="utf-8",
    )

    metadata_path = result.get("metadata_path")

    if metadata_path and Path(metadata_path).exists():
        metadata_file = Path(metadata_path)
        metadata = json.loads(
            metadata_file.read_text(encoding="utf-8")
        )

        metadata["status"] = "human_approved"
        metadata["approved_at"] = datetime.now(
            timezone.utc
        ).isoformat()
        metadata["approved_path"] = str(approved_path)

        metadata_file.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    logger.info("Newsletter approved and saved to %s", approved_path)

    return {
        **result,
        "status": "human_approved",
        "approved_path": str(approved_path),
    }


def reject_newsletter(result: dict) -> dict:
    """Reject a pending draft without marking it as approved."""
    if result.get("mode") != "human":
        raise ValueError("This newsletter was not created in human mode.")

    if result.get("status") != "pending_approval":
        raise ValueError("The newsletter is not awaiting approval.")

    metadata_path = result.get("metadata_path")

    if metadata_path and Path(metadata_path).exists():
        metadata_file = Path(metadata_path)
        metadata = json.loads(
            metadata_file.read_text(encoding="utf-8")
        )

        metadata["status"] = "human_rejected"
        metadata["rejected_at"] = datetime.now(
            timezone.utc
        ).isoformat()

        metadata_file.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    logger.info("Newsletter rejected by human reviewer")

    return {
        **result,
        "status": "human_rejected",
    }


def revise_rejected_newsletter(result: dict, feedback: str) -> dict:
    """Revise a pending newsletter using explicit human feedback."""

    if result.get("mode") != "human":
        raise ValueError(
            "Revisions are available only in human mode."
        )

    if result.get("status") != "pending_approval":
        raise ValueError(
            "Only a pending newsletter can be revised."
        )

    if not feedback or not feedback.strip():
        raise ValueError(
            "Please provide feedback before requesting a revision."
        )

    # Reuse the original research and selected articles.
    state = {
        **result,
        "human_feedback": feedback.strip(),
        "revision_count": result.get("revision_count", 0),
    }

    state["revision_count"] = result.get("revision_count", 0)
    # Rewrite and run the automated quality review.
    write_result = write_newsletter(state)
    state.update(write_result)

    review_result = review_newsletter(state)
    state.update(review_result)

    # Save the revised draft and keep it pending human approval.
    save_result = save_output(state)
    state.update(save_result)

    logger.info(
        "Revised newsletter saved for human approval | status=%s",
        state["status"],
    )

    return state


# ============================================================
# 15. OPTIONAL COMMAND-LINE TEST
# ============================================================

if __name__ == "__main__":
    goal = (
        "Create a newsletter covering the latest important general news "
        "for a broad audience. Include stories from categories such as "
        "politics, sports, festivals, technology, business, entertainment, "
        "science, and world events. Select up to 7 stories, ideally 5-7, "
        "with concise summaries and source links. Use fewer if fewer "
        "valid stories are available."
    )

    result = run_newsletter_agent(
        goal=goal,
        mode="autonomous",
    )

    print("\n========== NEWSLETTER RESULT ==========")
    print("Status:", result.get("status"))
    print("Subject:", result.get("subject"))
    print("Review:\n", result.get("critique"))
    print("HTML file:", result.get("output_path"))
    print("Metadata:", result.get("metadata_path"))
