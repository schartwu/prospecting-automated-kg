-- 9 Nov 2026, user decision: department discovery is done (4-digit sweep had nothing left to try; local
-- governments found 30 Oct-1 Nov). Paused, not removed: re-enable with cron.alter_job(..., active := true)
-- or run sweep.yml / local-sweep.yml by hand. units.py (inside daily.yml) keeps watching capped feeds.
select cron.alter_job(jobid, active := false) from cron.job where jobname in ('egp-night-sweep', 'egp-local-sweep');
