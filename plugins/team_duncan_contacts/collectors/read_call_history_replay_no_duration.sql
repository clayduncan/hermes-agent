-- Exact-event point lookup for a pending review row with no stored duration
-- (e.g. an unanswered call). ZDATE is matched within a small fixed
-- tolerance band (+/- 0.001s) rather than exact equality, to absorb
-- float/IEEE-754 and datetime-microsecond round-trip drift, not to select
-- among distinct calls -- the caller still verifies the returned row's
-- exact recomputed source_event_id. Omits the duration band entirely;
-- never hardcodes ZANSWERED, since an unanswered call is exactly what this
-- variant is for. All bound values via `.parameter set`, never concatenated.
SELECT ZDATE, ZADDRESS, ZDURATION, ZORIGINATED, ZANSWERED
FROM ZCALLRECORD
WHERE ZDATE BETWEEN :zdate_low AND :zdate_high
  AND ZORIGINATED = :zoriginated
  AND ZANSWERED = :zanswered;
