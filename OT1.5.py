# Initialize period boundaries and employee metadata
cr = payslip.env.cr
date_from = payslip.date_from
date_to = payslip.date_to
employee_id = employee.id
hours_per_day = 8.0

# Get employee calendar_id (for calendar-specific holiday filtering)
calendar_id = contract.resource_calendar_id.id or None

# -------------------------------------------------------------------------
# 1) Attendance minutes per day (SQL aggregated)
#    - Count from 18:01
#    - Truncate to minute precision
#    - No break deduction here
# -------------------------------------------------------------------------
sql_attendance = """
WITH att AS (
    SELECT
        (a.check_in + INTERVAL '7 hours')::date AS work_date,
        GREATEST(
            EXTRACT(EPOCH FROM (
                date_trunc('minute', a.check_out + INTERVAL '7 hours')
                -
                GREATEST(
                    date_trunc('minute', a.check_in + INTERVAL '7 hours'),
                    (a.check_in + INTERVAL '7 hours')::date + INTERVAL '18 hours 1 minute'
                )
            )) / 60.0,
            0
        ) AS att_minutes
    FROM hr_attendance a
    WHERE a.employee_id = %s
      AND (a.check_in + INTERVAL '7 hours')::date >= %s
      AND (a.check_in + INTERVAL '7 hours')::date <= %s
      AND a.check_out IS NOT NULL
      AND a.overtime_status = 'approved'
      -- Monday..Friday only
      AND EXTRACT(ISODOW FROM (a.check_in + INTERVAL '7 hours')) BETWEEN 1 AND 5
      -- Exclude public holidays
      AND NOT EXISTS (
          SELECT 1
          FROM resource_calendar_leaves cl
          WHERE cl.resource_id IS NULL
            AND cl.holiday_id IS NULL
            AND (cl.calendar_id IS NULL OR cl.calendar_id = %s)
            AND (a.check_in + INTERVAL '7 hours')::date
                BETWEEN (cl.date_from + INTERVAL '7 hours')::date
                    AND (cl.date_to + INTERVAL '7 hours')::date
      )
      -- Ignore sessions ending on or before 18:01
      AND (a.check_out + INTERVAL '7 hours')
          > (a.check_in + INTERVAL '7 hours')::date + INTERVAL '18 hours 1 minute'
)
SELECT work_date, SUM(att_minutes) AS att_minutes
FROM att
GROUP BY work_date
"""

cr.execute(sql_attendance, (employee_id, date_from, date_to, calendar_id))
att_rows = cr.fetchall()
att_dict = {work_date: float(att_minutes or 0.0) for work_date, att_minutes in att_rows}

# -------------------------------------------------------------------------
# 2) Request minutes per day (SQL aggregated)
#    - If request duration >= 2.0h, deduct 20 mins
# -------------------------------------------------------------------------
sql_request = """
WITH req AS (
    SELECT
        (r.start_date + INTERVAL '7 hours')::date AS work_date,
        GREATEST(
            CASE
                WHEN ROUND((EXTRACT(EPOCH FROM (r.end_date - r.start_date)) / 3600.0)::numeric, 2) >= 2.0
                    THEN ROUND(
                        (
                            ROUND((EXTRACT(EPOCH FROM (r.end_date - r.start_date)) / 3600.0)::numeric, 2)
                            - (20.0 / 60.0)
                        ) * 60.0
                    )
                ELSE ROUND(
                    ROUND((EXTRACT(EPOCH FROM (r.end_date - r.start_date)) / 3600.0)::numeric, 2) * 60.0
                )
            END,
            0
        ) AS req_minutes
    FROM overtime_request r
    WHERE r.employee_id = %s
      AND (r.start_date + INTERVAL '7 hours')::date >= %s
      AND (r.start_date + INTERVAL '7 hours')::date <= %s
      AND r.state = 'done'
      AND r.include_in_payroll = TRUE
      AND EXTRACT(ISODOW FROM (r.start_date + INTERVAL '7 hours')) BETWEEN 1 AND 5
      -- Exclude public holidays
      AND NOT EXISTS (
          SELECT 1
          FROM resource_calendar_leaves cl
          WHERE cl.resource_id IS NULL
            AND cl.holiday_id IS NULL
            AND (cl.calendar_id IS NULL OR cl.calendar_id = %s)
            AND (r.start_date + INTERVAL '7 hours')::date
                BETWEEN (cl.date_from + INTERVAL '7 hours')::date
                    AND (cl.date_to + INTERVAL '7 hours')::date
      )
)
SELECT work_date, SUM(req_minutes) AS req_minutes
FROM req
GROUP BY work_date
"""

cr.execute(sql_request, (employee_id, date_from, date_to, calendar_id))
req_rows = cr.fetchall()
req_dict = {work_date: float(req_minutes or 0.0) for work_date, req_minutes in req_rows}

# -------------------------------------------------------------------------
# 3) OT 1.5x rate setup
# -------------------------------------------------------------------------
hour_rate = float(contract.wage or 0.0) / 30.0 / hours_per_day
ot15_hour_rate = hour_rate * 1.5
ot15_min_rate = ot15_hour_rate / 60.0

# -------------------------------------------------------------------------
# 4) Strict day-by-day payout:
#    payable_minutes(day) = min(att_minutes(day), req_minutes(day))
#    then calculate and sum each day amount
# -------------------------------------------------------------------------
all_dates = sorted(set(att_dict.keys()) | set(req_dict.keys()))
total_amount = 0.0
total_ot_minutes = 0

for work_date in all_dates:
    att_m = max(float(att_dict.get(work_date, 0.0)), 0.0)
    req_m = max(float(req_dict.get(work_date, 0.0)), 0.0)

    least_m = min(att_m, req_m)

    # Split per day (Excel-like)
    h = int(least_m // 60)
    m = int(least_m % 60)

    daily_amount = (h * ot15_hour_rate) + (m * ot15_min_rate)
    total_amount += daily_amount
    total_ot_minutes += (h * 60 + m)

# Final payroll rule outputs
result = total_amount
result_qty = total_ot_minutes / 60.0
result_name = "Basic Salary Overtime 1.5x"
