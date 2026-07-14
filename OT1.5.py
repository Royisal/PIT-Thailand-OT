# Initialize period boundaries and employee metadata
cr = payslip.env.cr
date_from = payslip.date_from
date_to = payslip.date_to
employee_id = employee.id
hours_per_day = 8.0

# Get the employee's calendar_id to check calendar-specific holidays
calendar_id = contract.resource_calendar_id.id or None

# 1. SQL Query to get daily attendance minutes (starting from 18:01, truncated to minutes, NO break deduction)
sql_attendance = """
SELECT 
    (a.check_in + INTERVAL '7 hours')::date as work_date,
    EXTRACT(EPOCH FROM (
        date_trunc('minute', a.check_out + INTERVAL '7 hours')
        -
        GREATEST(date_trunc('minute', a.check_in + INTERVAL '7 hours'), (a.check_in + INTERVAL '7 hours')::date + INTERVAL '18 hours 1 minute')
    )) / 60.0 as att_minutes
FROM hr_attendance a
WHERE a.employee_id = %s
  AND (a.check_in + INTERVAL '7 hours')::date >= %s
  AND (a.check_in + INTERVAL '7 hours')::date <= %s
  AND a.check_out IS NOT NULL
  AND a.overtime_status = 'approved'
  -- Filter strictly for weekdays: Monday to Friday (ISODOW 1 to 5)
  AND EXTRACT(ISODOW FROM (a.check_in + INTERVAL '7 hours')) BETWEEN 1 AND 5
  -- EXCLUDE PUBLIC HOLIDAYS
  AND NOT EXISTS (
      SELECT 1 FROM resource_calendar_leaves cl
      WHERE cl.resource_id IS NULL
        AND cl.holiday_id IS NULL
        AND (cl.calendar_id IS NULL OR cl.calendar_id = %s)
        AND (a.check_in + INTERVAL '7 hours')::date BETWEEN (cl.date_from + INTERVAL '7 hours')::date AND (cl.date_to + INTERVAL '7 hours')::date
  )
  -- Cutoff to ignore check-outs on or before 18:01
  AND (a.check_out + INTERVAL '7 hours') > (a.check_in + INTERVAL '7 hours')::date + INTERVAL '18 hours 1 minute'
"""

cr.execute(sql_attendance, (employee_id, date_from, date_to, calendar_id))
att_rows = cr.fetchall()
att_dict = {row[0]: float(row[1]) for row in att_rows}

# 2. SQL Query to get daily request minutes (starting from 18:01, with break deduction, rounded to minutes)
sql_request = """
SELECT 
    (r.start_date + INTERVAL '7 hours')::date as work_date,
    ROUND((EXTRACT(EPOCH FROM (r.end_date - r.start_date)) / 3600.0)::numeric, 2) as req_hours
FROM overtime_request r
WHERE r.employee_id = %s
  AND (r.start_date + INTERVAL '7 hours')::date >= %s
  AND (r.start_date + INTERVAL '7 hours')::date <= %s
  AND r.state = 'done'
  AND r.include_in_payroll = TRUE
  AND EXTRACT(ISODOW FROM (r.start_date + INTERVAL '7 hours')) BETWEEN 1 AND 5
  -- EXCLUDE PUBLIC HOLIDAYS
  AND NOT EXISTS (
      SELECT 1 FROM resource_calendar_leaves cl
      WHERE cl.resource_id IS NULL
        AND cl.holiday_id IS NULL
        AND (cl.calendar_id IS NULL OR cl.calendar_id = %s)
        AND (r.start_date + INTERVAL '7 hours')::date BETWEEN (cl.date_from + INTERVAL '7 hours')::date AND (cl.date_to + INTERVAL '7 hours')::date
  )
"""

cr.execute(sql_request, (employee_id, date_from, date_to, calendar_id))
req_rows = cr.fetchall()

req_dict = {}
for row in req_rows:
    w_date, req_hours = row
    # Apply 20-minute break deduction to request minutes if req_hours >= 2.0
    if req_hours >= 2.0:
        req_mins = round((req_hours - (20.0 / 60.0)) * 60.0)
    else:
        req_mins = round(req_hours * 60.0)
    req_dict[w_date] = req_mins

# 3. Calculate daily LEAST minutes and sum them up row by row (splitting hours and minutes)
total_hours = 0
total_minutes = 0
all_dates = set(list(att_dict.keys()) + list(req_dict.keys()))

for w_date in all_dates:
    att_m = att_dict.get(w_date, 0.0)
    req_m = req_dict.get(w_date, 0.0)
    least_m = min(att_m, req_m)
    
    # Split into hours and minutes row by row
    h = int(least_m // 60)
    m = int(least_m % 60)
    total_hours += h
    total_minutes += m

# -------------------------------------------------------------------------
# Final Calculation (Weekday OT Rate = 1.5x)
# -------------------------------------------------------------------------
hour_rate = float(contract.wage or 0.0) / 30.0 / hours_per_day
ot15_hour_rate = hour_rate * 1.5
ot15_min_rate = ot15_hour_rate / 60.0

# Calculate final amount separately for hours and minutes and then sum (matches Excel)
result = (total_hours * ot15_hour_rate) + (total_minutes * ot15_min_rate)
# result_qty = (total_hours * 60 + total_minutes) / 60.0
result_name = "Basic Salary Overtime 1.5x"
