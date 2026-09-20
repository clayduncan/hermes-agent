-- Exact-event point lookup for a pending review row with no stored duration
-- (e.g. an unanswered call). Omits the duration band entirely; never
-- hardcodes ZANSWERED, since an unanswered call is exactly what this
-- variant is for. All bound values via `.parameter set`, never concatenated.
SELECT ZDATE, ZADDRESS, ZDURATION, ZORIGINATED, ZANSWERED
FROM ZCALLRECORD
WHERE ZDATE = :target_zdate
  AND ZORIGINATED = :zoriginated
  AND ZANSWERED = :zanswered;
