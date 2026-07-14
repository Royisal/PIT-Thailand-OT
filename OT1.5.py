# Initialize period boundaries and employee metadata
cr = payslip.env.cr
date_from = payslip.date_from
date_to = payslip.date_to
employee_id = employee.id
hours_per_day = 8.0

# Get the employee's calendar_id to check calendar-specific holidays (as well as global public holidays where calendar_id is NULL)
calendar_id = contract.resource_calendar_id.id or None

# -------------------------------------------------------------------------
# Overtime: Capped 08:00 - 17:00 & Sat-Sun/Holiday & Approved (Compare Attendance vs Request)
# -------------------------------------------------------------------------
sql_attendance = """
WITH attendance_ot AS (
    SELECT 
        work_date,
        SUM(
            CASE 
                WHEN ROUND(raw_hours::numeric, 1) >= 8.0 
                THEN ROUND(raw_hours::numeric, 1) - 1.0 
                ELSE ROUND(raw_hours::numeric, 1) 
            END
        ) as att_ot_hours
    FROM (
        SELECT 
            (a.check_in + INTERVAL '7 hours')::date as work_date,
            GREATEST(
                EXTRACT(EPOCH FROM (
                    LEAST(MAX(a.check_out + INTERVAL '7 hours'), (a.check_in + INTERVAL '7 hours')::date + INTERVAL '17 hours')
                    -
                    GREATEST(MIN(a.check_in + INTERVAL '7 hours'), (a.check_in + INTERVAL '7 hours')::date + INTERVAL '08 hours')
                )) / 3600.0,
            0.0) as raw_hours
        FROM hr_attendance a
        WHERE a.employee_id = %s
          AND (a.check_in + INTERVAL '7 hours')::date >= %s
          AND (a.check_in + INTERVAL '7 hours')::date <= %s
          AND a.check_out IS NOT NULL
          AND a.overtime_status = 'approved'
          AND (
            EXTRACT(ISODOW FROM (a.check_in + INTERVAL '7 hours')) BETWEEN 6 AND 7
            OR EXISTS (
                SELECT 1 FROM resource_calendar_leaves cl
                WHERE cl.resource_id IS NULL
                  AND cl.holiday_id IS NULL
                  AND (cl.calendar_id IS NULL OR cl.calendar_id = %s)
                  AND (a.check_in + INTERVAL '7 hours')::date BETWEEN (cl.date_from + INTERVAL '7 hours')::date AND (cl.date_to + INTERVAL '7 hours')::date
            )
          )
        GROUP BY (a.check_in + INTERVAL '7 hours')::date
    ) sub
    GROUP BY work_date
),
request_ot AS (
    SELECT 
        (r.start_date + INTERVAL '7 hours')::date as work_date,
        SUM(EXTRACT(EPOCH FROM (r.end_date - r.start_date)) / 3600.0) as req_ot_hours
    FROM overtime_request r
    WHERE r.employee_id = %s
      AND (r.start_date + INTERVAL '7 hours')::date >= %s
      AND (r.start_date + INTERVAL '7 hours')::date <= %s
      AND r.state = 'done'
      AND r.include_in_payroll = TRUE
      AND (
        EXTRACT(ISODOW FROM (r.start_date + INTERVAL '7 hours')) BETWEEN 6 AND 7
        OR EXISTS (
            SELECT 1 FROM resource_calendar_leaves cl
            WHERE cl.resource_id IS NULL
              AND cl.holiday_id IS NULL
              AND (cl.calendar_id IS NULL OR cl.calendar_id = %s)
              AND (r.start_date + INTERVAL '7 hours')::date BETWEEN (cl.date_from + INTERVAL '7 hours')::date AND (cl.date_to + INTERVAL '7 hours')::date
        )
      )
    GROUP BY (r.start_date + INTERVAL '7 hours')::date
)
SELECT COALESCE(a_ot.work_date, r_ot.work_date) as work_date,
       a_ot.att_ot_hours,
       r_ot.req_ot_hours
FROM attendance_ot a_ot
LEFT JOIN request_ot r_ot ON a_ot.work_date = r_ot.work_date
"""

# Execute the query (passing exactly 8 parameters matching the 8 '%s' placeholders)
cr.execute(
    sql_attendance,
    (
        employee_id, date_from, date_to, calendar_id,
        employee_id, date_from, date_to, calendar_id
    ),
)
rows = cr.fetchall()

# 3. Calculate daily LEAST minutes and sum them up strictly (no requests = 0 OT hours)
total_hours = 0
total_minutes = 0

for row in rows:
    w_date, att_h, req_h = row
    att_h = float(att_h or 0.0)
    req_h = float(req_h or 0.0) if req_h is not None else 0.0
    
    # Strictly take the LEAST between actual attendance and approved request
    least_h = min(att_h, req_h)
        
    minutes = round(least_h * 60.0)
    total_hours += int(minutes // 60)
    total_minutes += int(minutes % 60)

# -------------------------------------------------------------------------
# Final Calculation (Weekend/Holiday OT Rate = 1.0x)
# -------------------------------------------------------------------------
hour_rate = float(contract.wage or 0.0) / 30.0 / hours_per_day
ot1_hour_rate = hour_rate * 1.0
ot1_min_rate = ot1_hour_rate / 60.0

# Calculate final amount separately for hours and minutes and then sum (matches Excel)
result = (total_hours * ot1_hour_rate) + (total_minutes * ot1_min_rate)
# result_qty = (total_hours * 60 + total_minutes) / 60.0
result_name = "Basic Salary Weekend/Holiday Overtime 1.0x"
