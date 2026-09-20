-- Fixed, non-templated routine scan: the last CALL_HISTORY_LOOKBACK_DAYS days
-- of call history, oldest first. :cutoff_apple_epoch is bound via the
-- sqlite3 CLI's `.parameter set`, never string-concatenated.
SELECT ZDATE, ZADDRESS, ZDURATION, ZORIGINATED, ZANSWERED
FROM ZCALLRECORD
WHERE ZDATE >= :cutoff_apple_epoch
ORDER BY ZDATE ASC;
