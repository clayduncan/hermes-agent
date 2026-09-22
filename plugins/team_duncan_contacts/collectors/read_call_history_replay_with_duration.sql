-- Exact-event point lookup, used when the pending review row has a known
-- duration. ZDATE is matched within a small fixed tolerance band
-- (+/- 0.001s) rather than exact equality, to absorb float/IEEE-754 and
-- datetime-microsecond round-trip drift, not to select among distinct
-- calls -- the caller still verifies the returned row's exact recomputed
-- source_event_id. Duration is matched within a small fixed tolerance band
-- (duration_s +/- 2 seconds) rather than exact equality, to absorb minor
-- source rounding. All bound values via `.parameter set`, never concatenated.
SELECT ZDATE, ZADDRESS, ZDURATION, ZORIGINATED, ZANSWERED
FROM ZCALLRECORD
WHERE ZDATE BETWEEN :zdate_low AND :zdate_high
  AND ZDURATION BETWEEN :duration_low AND :duration_high
  AND ZORIGINATED = :zoriginated
  AND ZANSWERED = :zanswered;
