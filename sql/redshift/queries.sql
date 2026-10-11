-- ADR-0023: the five queries. Shared SQL: the local Postgres runs this same file in the tests,
-- Redshift runs it in the first session, and the results are compared. Only ecp_* tables.
-- Every price is a modelled estimate, not a market quote (ADR-0002).

-- name: latest_curve_per_series
-- The newest complete curve of each curve id: every position of its latest as-of date.
SELECT c.curve_id, c.as_of_date, c."position", c.price
FROM ecp_curve_points c
JOIN (
    SELECT curve_id, MAX(as_of_date) AS as_of_date
    FROM ecp_curve_points
    WHERE status = 'ok'
    GROUP BY curve_id
) latest ON latest.curve_id = c.curve_id AND latest.as_of_date = c.as_of_date
WHERE c.status = 'ok'
ORDER BY c.curve_id, c."position";

-- name: wti_brent_spread_by_tenor
-- Brent minus WTI at each tenor and date, where both curves have a price.
SELECT b.as_of_date, b."position", b.price - w.price AS spread
FROM ecp_curve_points b
JOIN ecp_curve_points w
  ON w.as_of_date = b.as_of_date AND w."position" = b."position"
 AND w.curve_id = 'WTI' AND w.status = 'ok'
WHERE b.curve_id = 'BRENT' AND b.status = 'ok'
ORDER BY b.as_of_date, b."position";

-- name: contango_backwardation_days
-- Days on which the fourth contract is above (contango) or below (backwardation) spot.
SELECT s.curve_id,
       SUM(CASE WHEN f.price > s.price THEN 1 ELSE 0 END) AS contango_days,
       SUM(CASE WHEN f.price < s.price THEN 1 ELSE 0 END) AS backwardation_days
FROM ecp_curve_points s
JOIN ecp_curve_points f
  ON f.curve_id = s.curve_id AND f.as_of_date = s.as_of_date AND f."position" = 'C4'
 AND f.status = 'ok'
WHERE s."position" = 'Spot' AND s.status = 'ok' AND s.kind = 'outright'
GROUP BY s.curve_id
ORDER BY s.curve_id;

-- name: revisions_per_month
-- How many published prices were later revised, by the month of the observation.
SELECT TO_CHAR(observation_date, 'YYYY-MM') AS month, series_id, COUNT(*) AS revisions
FROM ecp_revisions
GROUP BY TO_CHAR(observation_date, 'YYYY-MM'), series_id
ORDER BY 1, 2;

-- name: rolling_percentile
-- Where each WTI price sits among its curve position's prices in the same calendar month, 0 for
-- the lowest and 1 for the highest. The one window function. (A rank function ignores a window
-- frame, and PERCENTILE_CONT is not a Postgres window function, so a true N-day rolling
-- percentile is not shared SQL; the month is the window. ADR-0023.)
SELECT curve_id, "position", as_of_date, price,
       PERCENT_RANK() OVER (
           PARTITION BY curve_id, "position", TO_CHAR(as_of_date, 'YYYY-MM')
           ORDER BY price
       ) AS month_percent_rank
FROM ecp_curve_points
WHERE curve_id = 'WTI' AND status = 'ok'
ORDER BY curve_id, "position", as_of_date;
