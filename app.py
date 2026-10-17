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
        "Tech news and prepare it for subscribers."
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
            "Starting Newsletter Agent...",
            expanded=True
        ) as status:

            def report_progress(message):
                status.write(message)

            result = run_newsletter_agent(
                goal,
                mode=(
                    "autonomous"
                    if mode == "Fully Autonomous"
                    else "human"
                ),
                progress_callback=report_progress,
            )

            st.session_state["newsletter_result"] = result
            status.update(
                label="Newsletter generation complete",
                state="complete",
                expanded=False,
            )

    except Exception as exc:
        st.error(f"Newsletter generation failed: {exc}")

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

    # Show downloads whenever newsletter HTML is available
    if html and html.strip():
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
    else:
        st.info("Downloads will appear once the newsletter has been generated.")
