# Initialize period boundaries and employee metadata
cr = payslip.env.cr
date_from = payslip.date_from
date_to = payslip.date_to
employee_id = employee.id
hours_per_day = 8.0

# Get the employee's calendar_id to check calendar-specific holidays (as well as global public holidays where calendar_id is NULL)
calendar_id = contract.resource_calendar_id.id or None

# -------------------------------------------------------------------------
# Overtime: After 17:01 on Sat-Sun/Holiday & Approved (Compare Attendance vs Request)
# -------------------------------------------------------------------------
sql_attendance = """
WITH attendance_ot AS (
    SELECT 
        (a.check_in + INTERVAL '7 hours')::date as work_date,
        EXTRACT(EPOCH FROM (
            date_trunc('minute', a.check_out + INTERVAL '7 hours')
            -
            GREATEST(date_trunc('minute', a.check_in + INTERVAL '7 hours'), (a.check_in + INTERVAL '7 hours')::date + INTERVAL '17 hours 1 minute')
        )) / 60.0 as raw_att_minutes
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
      AND (a.check_out + INTERVAL '7 hours') > (a.check_in + INTERVAL '7 hours')::date + INTERVAL '17 hours 1 minute'
),
request_ot AS (
    SELECT 
        (r.start_date + INTERVAL '7 hours')::date as work_date,
        ROUND(
            GREATEST(
                EXTRACT(EPOCH FROM (
                    (r.end_date + INTERVAL '7 hours')
                    -
                    GREATEST((r.start_date + INTERVAL '7 hours'), (r.start_date + INTERVAL '7 hours')::date + INTERVAL '17 hours 1 minute')
                )) / 3600.0,
            0.0)::numeric,
        2) as req_ot3_hours
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
      AND (r.end_date + INTERVAL '7 hours') > (r.start_date + INTERVAL '7 hours')::date + INTERVAL '17 hours 1 minute'
)
SELECT COALESCE(a_ot.work_date, r_ot.work_date) as work_date,
       a_ot.raw_att_minutes,
       r_ot.req_ot3_hours
FROM attendance_ot a_ot
LEFT JOIN request_ot r_ot ON a_ot.work_date = r_ot.work_date
"""

# Execute the query (passing exactly 8 parameters matching the 8 placeholders)
cr.execute(
    sql_attendance,
    (
        employee_id, date_from, date_to, calendar_id,
        employee_id, date_from, date_to, calendar_id
    ),
)
rows = cr.fetchall()

# Calculate daily LEAST minutes with 20 minutes break deduction
att_dict = {}
req_dict = {}

for row in rows:
    w_date, raw_att_m, req_h = row
    
    # 1. Process Attendance: Deduct 20 mins break if raw minutes >= 120
    raw_att_m = float(raw_att_m or 0.0)
    if raw_att_m >= 120.0:
        att_mins = raw_att_m - 20.0
    else:
        att_mins = raw_att_m
    att_dict[w_date] = att_mins
    
    # 2. Process Request: Deduct 20 mins break if raw requested minutes >= 120
    req_h = float(req_h or 0.0) if req_h is not None else 0.0
    raw_req_m = round(req_h * 60.0)
    if raw_req_m >= 120.0:
        req_mins = raw_req_m - 20.0
    else:
        req_mins = raw_req_m
    req_dict[w_date] = req_mins

# 3. Calculate daily LEAST minutes and sum them up
total_hours = 0
total_minutes = 0
all_dates = set(list(att_dict.keys()) + list(req_dict.keys()))

for w_date in all_dates:
    att_m = att_dict.get(w_date, 0.0)
    req_m = req_dict.get(w_date, 0.0)
    
    # Strictly take the LEAST between attendance and request
    least_m = min(att_m, req_m)
    
    total_hours += int(least_m // 60)
    total_minutes += int(least_m % 60)

# -------------------------------------------------------------------------
# Final Calculation (Weekend/Holiday OT Rate = 3.0x)
# -------------------------------------------------------------------------
hour_rate = float(contract.wage or 0.0) / 30.0 / hours_per_day
ot3_hour_rate = hour_rate * 3.0
ot3_min_rate = ot3_hour_rate / 60.0

# Calculate final amount separately for hours and minutes and then sum (matches Excel)
result = (total_hours * ot3_hour_rate) + float(total_minutes * ot3_min_rate)
result_name = "Basic Salary Weekend/Holiday Overtime 3.0x"
