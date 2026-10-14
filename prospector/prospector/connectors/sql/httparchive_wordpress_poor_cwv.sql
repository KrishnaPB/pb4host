-- WordPress origins in one country whose real-user mobile LCP (p75) is "poor" (> 4000 ms) for a month.
-- Sources: HTTP Archive monthly crawl (technology detection) joined with the CrUX country summary.
-- Parameters: @month DATE (first day of the crawl month), @yyyymm INT64, @country STRING (lower-case ISO),
--             @lcp_threshold_ms INT64, @limit INT64.
-- Verify the column names against the current schemas before the first production run
-- (httparchive.crawl.pages and chrome-ux-report.materialized.country_summary evolve).
WITH crux AS (
  SELECT origin, p75_lcp, p75_inp, p75_cls, rank
  FROM `chrome-ux-report.materialized.country_summary`
  WHERE yyyymm = @yyyymm
    AND country_code = @country
    AND device = 'phone'
    AND p75_lcp IS NOT NULL
    AND p75_lcp > @lcp_threshold_ms
),
wp AS (
  SELECT DISTINCT RTRIM(page, '/') AS origin
  FROM `httparchive.crawl.pages`
  WHERE date = @month
    AND client = 'mobile'
    AND is_root_page
    AND EXISTS (SELECT 1 FROM UNNEST(technologies) AS t WHERE t.technology = 'WordPress')
)
SELECT crux.origin, crux.p75_lcp AS lcp_p75, crux.p75_inp AS inp_p75, crux.p75_cls AS cls_p75, crux.rank
FROM crux
JOIN wp USING (origin)
ORDER BY crux.rank ASC, crux.p75_lcp DESC
LIMIT @limit
