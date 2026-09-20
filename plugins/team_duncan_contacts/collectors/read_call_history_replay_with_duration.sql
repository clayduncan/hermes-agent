-- Exact-event point lookup, used when the pending review row has a known
-- duration. Duration is matched within a small fixed tolerance band
-- (duration_s +/- 2 seconds) rather than exact equality, to absorb minor
-- source rounding. All bound values via `.parameter set`, never concatenated.
SELECT ZDATE, ZADDRESS, ZDURATION, ZORIGINATED, ZANSWERED
FROM ZCALLRECORD
WHERE ZDATE = :target_zdate
  AND ZDURATION BETWEEN :duration_low AND :duration_high
  AND ZORIGINATED = :zoriginated
  AND ZANSWERED = :zanswered;
