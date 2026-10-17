import streamlit as st
from pathlib import Path
from agent import (
    run_newsletter_agent,
    retry_newsletter_writing,
    approve_newsletter,
    reject_newsletter,
    revise_rejected_newsletter,
)
from download_utils import create_pdf, create_word

st.set_page_config(
    page_title="Newsletter Agent",
    page_icon="📰",
    layout="wide",
)

if "newsletter_result" not in st.session_state:
    st.session_state["newsletter_result"] = None

st.title("📰 AI Newsletter Agent")
st.caption(
    "Research. Summarize. Review. Generate."
)

goal = st.text_area(
    "What should the agent do?",
    value=(
        "Create a weekly newsletter on the latest "
        "AI agent news and prepare it for subscribers."
    ),
    height=100,
)

mode = st.radio(
    "Execution mode",
    ["Fully Autonomous", "Human-in-the-Loop"],
    horizontal=True,
)

if st.button("Run Newsletter Agent", type="primary"):
    try:
        with st.status(
            "Agent is working...",
            expanded=True,
        ) as status:
            st.write("Planning the task...")
            st.write("Searching for recent articles...")
            st.write("Writing and reviewing the newsletter...")

            result = run_newsletter_agent(
                goal,
                mode=(
                    "autonomous"
                    if mode == "Fully Autonomous"
                    else "human"
                ),
            )

            status.update(
                label="Newsletter draft ready",
                state="complete",
            )

        st.session_state["newsletter_result"] = result

    except Exception as exc:
        st.error(f"Agent failed: {exc}")

result = st.session_state.get("newsletter_result")

if result:
    st.subheader("Execution details")
    st.write("Status:", result.get("status"))
    st.write("Plan:", result.get("plan", ""))
    st.write("Review:", result.get("critique", ""))

    st.subheader("Research results")

    for article in result.get("articles", []):
        st.markdown(
            f"- [{article['title']}]({article['url']})"
        )

    
    if result.get("articles"):
        if st.button("Retry Newsletter Writing"):
            try:
                with st.spinner(
                    "Retrying with previously selected articles..."
                ):
                    updated_result = retry_newsletter_writing(
                        st.session_state["newsletter_result"]
                    )

                st.session_state["newsletter_result"] = updated_result

                st.success("Newsletter writing completed.")
                st.rerun()

            except Exception as exc:
                st.error(f"Writing retry failed: {exc}")


    st.subheader("Newsletter preview")

    html = result.get("newsletter", "")

    st.components.v1.html(
        html,
        height=650,
        scrolling=True,
    )

    
    # Human-in-the-Loop approval

    if result.get("status") == "pending_approval":
        st.subheader("Human Review")

        feedback = st.text_area(
            "Revision feedback",
            placeholder=(
                "Example: Use more recent AI agent news, "
                "shorten the summaries, and improve readability."
            ),
            key=(
                f"feedback_{result.get('run_id', 'newsletter')}_"
                f"{result.get('revision_count', 0)}"
            ),
            height=100,
        )

        col1, col2, col3 = st.columns(3)

        with col1:
            if st.button(
                "Approve and Save",
                type="primary",
                key="approve_newsletter",
            ):
                try:
                    updated = approve_newsletter(result)
                    st.session_state["newsletter_result"] = updated
                    st.rerun()
                except Exception as exc:
                    st.error(f"Approval failed: {exc}")

        with col2:
            if st.button(
                "Reject and Revise",
                key="revise_newsletter",
            ):
                if not feedback.strip():
                    st.warning(
                        "Please enter feedback before requesting a revision."
                    )
                else:
                    try:
                        with st.spinner("Revising newsletter..."):
                            updated = revise_rejected_newsletter(
                                result,
                                feedback.strip(),
                            )

                        st.session_state["newsletter_result"] = updated
                        st.rerun()

                    except Exception as exc:
                        st.error(f"Revision failed: {exc}")

        with col3:
            if st.button(
                "Reject Permanently",
                key="reject_newsletter",
            ):
                try:
                    updated = reject_newsletter(result)
                    st.session_state["newsletter_result"] = updated
                    st.rerun()
                except Exception as exc:
                    st.error(f"Rejection failed: {exc}")

    elif result.get("status") == "human_approved":
        st.success("Newsletter approved and saved.")

    elif result.get("status") == "review_passed":
        st.success("Newsletter passed automated review.")

    elif result.get("status") == "human_rejected":
        st.warning("This newsletter has been rejected.")

    # Downloads are independent of the approval status messages
    if result.get("status") in {"human_approved", "review_passed"} and html:
        st.subheader("Download Newsletter")

        try:
            pdf_data = create_pdf(html)
            word_data = create_word(html)

            col1, col2, col3 = st.columns(3)

            with col1:
                st.download_button(
                    "Download newsletter HTML",
                    data=html.encode("utf-8"),
                    file_name="newsletter.html",
                    mime="text/html",
                    use_container_width=True,
                    key="download_newsletter_html",
                )

            with col2:
                st.download_button(
                    "Download newsletter PDF",
                    data=pdf_data,
                    file_name="newsletter.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                    key="download_newsletter_pdf",
                )

            with col3:
                st.download_button(
                    "Download newsletter Word",
                    data=word_data,
                    file_name="newsletter.docx",
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                    use_container_width=True,
                    key="download_newsletter_word",
                )

        except Exception as exc:
            st.error(f"Could not prepare download files: {exc}")

    elif result.get("status") in {"human_approved", "review_passed"}:
        st.warning("The newsletter HTML content is empty.")
