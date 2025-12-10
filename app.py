# Run button
if st.button("🔍 Run lookup", type="primary"):
    if not addresses:
        st.warning("Please paste at least one address or upload a CSV.")
        st.stop()

    st.info(f"Looking up {len(addresses)} addresses…")

    results_df, error_list = build_results(addresses, years)

    if error_list:
        with st.expander("Show API errors (for debugging)"):
            for msg in error_list:
                st.error(msg)

    st.success("Lookup complete!")

    st.subheader("Results")

    if results_df.empty:
        st.write("No results returned.")
    else:
        # (results rendering here...)
        ...
else:
    st.info(
        "Paste addresses or upload a CSV, select years, then click **Run lookup**."
    )
