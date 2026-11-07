
from datetime import datetime, date
import json
import os
import uuid
from typing import List, Optional

import psycopg
from psycopg.rows import dict_row
from mcp.server import MCPServer
from pathlib import Path
from dotenv import load_dotenv

mcp = MCPServer("LeaveManagementSystem")


# ---------------------------------------------------------
# PostgreSQL Connection
# ---------------------------------------------------------

# Resolve project root regardless of where MCP is launched from.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load environment variables from this project's .env file.
load_dotenv(PROJECT_ROOT / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing from the project .env file.")


def get_db_connection():
    """Create a PostgreSQL connection."""
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row
    )


# ---------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------

def get_business_dates(start_str: str, end_str: str) -> List[str]:
    """Return working days (Monday-Friday), inclusive."""

    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end = datetime.strptime(end_str, "%Y-%m-%d").date()

    if start > end:
        raise ValueError("start_date cannot be after end_date.")

    business_dates = []
    current = start

    while current <= end:
        if current.weekday() < 5:
            business_dates.append(current.isoformat())

        current = date.fromordinal(current.toordinal() + 1)

    return business_dates


def employee_exists(conn, employee_id: str) -> bool:
    result = conn.execute(
        """
        SELECT 1
        FROM employees
        WHERE employee_id = %s
        """,
        (employee_id.upper(),)
    ).fetchone()

    return result is not None


# ---------------------------------------------------------
# Resources
# ---------------------------------------------------------

@mcp.resource("policy://company/leave")
def get_leave_policy() -> str:
    """Retrieve official company leave policies and entitlements."""

    return (
        "# Standard Leave Policy\n"
        "- Annual Vacation: 20 days/year\n"
        "- Paid Sick Leave: 10 days/year "
        "(Doctor's note required for > 3 days)\n"
        "- Personal Leave: 5 days/year "
        "(Requires 48h advance notice)\n"
        "- Weekends (Saturday & Sunday) are non-deductible."
    )


@mcp.resource("balances://{employee_id}")
def get_employee_balance_resource(employee_id: str) -> str:
    """Expose leave balances for a specific employee."""

    employee_id = employee_id.upper()

    with get_db_connection() as conn:
        if not employee_exists(conn, employee_id):
            return f"Employee '{employee_id}' not found."

        balances = conn.execute(
            """
            SELECT leave_type, available_days
            FROM leave_balances
            WHERE employee_id = %s
            ORDER BY leave_type
            """,
            (employee_id,)
        ).fetchall()

    return json.dumps(
        {
            row["leave_type"].lower(): row["available_days"]
            for row in balances
        },
        indent=2
    )


# ---------------------------------------------------------
# Tool 1: Check Leave Balance
# ---------------------------------------------------------

@mcp.tool()
def check_leave_balance(employee_id: str) -> str:
    """Check remaining leave days for an employee."""

    employee_id = employee_id.upper()

    with get_db_connection() as conn:
        employee = conn.execute(
            """
            SELECT employee_id, name, department
            FROM employees
            WHERE employee_id = %s
            """,
            (employee_id,)
        ).fetchone()

        if not employee:
            return f"Error: Employee '{employee_id}' does not exist."

        balances = conn.execute(
            """
            SELECT leave_type, available_days
            FROM leave_balances
            WHERE employee_id = %s
            ORDER BY leave_type
            """,
            (employee_id,)
        ).fetchall()

    result = {
        "employee_id": employee["employee_id"],
        "name": employee["name"],
        "department": employee["department"],
        "balances": {
            row["leave_type"].lower(): row["available_days"]
            for row in balances
        }
    }

    return json.dumps(result, indent=2)


# ---------------------------------------------------------
# Tool 2: Submit Leave Request
# ---------------------------------------------------------

@mcp.tool()
def submit_leave_request(
    employee_id: str,
    leave_type: str,
    start_date: str,
    end_date: str,
    reason: str
) -> str:
    """Apply for a new leave request."""

    employee_id = employee_id.upper()
    leave_type = leave_type.upper()

    allowed_types = ["VACATION", "SICK", "PERSONAL"]

    if leave_type not in allowed_types:
        return (
            "Error: Invalid leave type. "
            "Allowed: vacation, sick, personal."
        )

    try:
        # Validate dates and calculate working days.
        leave_dates = get_business_dates(start_date, end_date)

    except ValueError as error:
        return f"Date Error: {str(error)}"

    days_requested = len(leave_dates)

    if days_requested == 0:
        return "Error: Leave period contains 0 business days."

    try:
        with get_db_connection() as conn:

            employee = conn.execute(
                """
                SELECT employee_id, name, department
                FROM employees
                WHERE employee_id = %s
                """,
                (employee_id,)
            ).fetchone()

            if not employee:
                return f"Error: Employee '{employee_id}' not found."

            balance = conn.execute(
                """
                SELECT available_days
                FROM leave_balances
                WHERE employee_id = %s
                  AND leave_type = %s
                FOR UPDATE
                """,
                (employee_id, leave_type)
            ).fetchone()

            if not balance:
                return f"Error: No {leave_type.lower()} balance configured."

            if balance["available_days"] < days_requested:
                return (
                    f"Error: Insufficient balance. "
                    f"Requested: {days_requested} days, "
                    f"Available: {balance['available_days']} days."
                )

            # Prevent overlapping pending or approved leave dates.
            conflict = conn.execute(
                """
                SELECT lr.request_id
                FROM leave_request_dates lrd
                JOIN leave_requests lr
                    ON lr.request_id = lrd.request_id
                WHERE lr.employee_id = %s
                  AND lr.status IN ('PENDING', 'APPROVED')
                  AND lrd.leave_date = ANY(%s::date[])
                LIMIT 1
                """,
                (employee_id, [date.fromisoformat(d) for d in leave_dates])
            ).fetchone()

            if conflict:
                return (
                    "Error: These dates overlap with an existing "
                    f"pending or approved request ({conflict['request_id']})."
                )

            # UUID-based ID avoids collisions between concurrent submissions.
            request_id = f"REQ-{uuid.uuid4().hex.upper()}"

            conn.execute(
                """
                INSERT INTO leave_requests (
                    request_id,
                    employee_id,
                    leave_type,
                    start_date,
                    end_date,
                    days_requested,
                    reason,
                    status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
                """,
                (
                    request_id,
                    employee_id,
                    leave_type,
                    start_date,
                    end_date,
                    days_requested,
                    reason
                )
            )

            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO leave_request_dates (request_id, leave_date)
                    VALUES (%s, %s)
                    """,
                    [
                        (request_id, date.fromisoformat(d))
                        for d in leave_dates
                    ]
                )

            request_data = {
                "request_id": request_id,
                "employee_id": employee_id,
                "employee_name": employee["name"],
                "leave_type": leave_type,
                "start_date": start_date,
                "end_date": end_date,
                "days_requested": days_requested,
                "leave_dates": leave_dates,
                "reason": reason,
                "status": "PENDING"
            }

    except Exception as error:
        import traceback
        traceback.print_exc()
        return f"Database Error: {type(error).__name__}: {error}"
    return (
        "Request submitted successfully:\n"
        + json.dumps(request_data, indent=2)
    )


# ---------------------------------------------------------
# Tool 3: Review Leave Request
# ---------------------------------------------------------

@mcp.tool()
def review_leave_request(
    request_id: str,
    action: str,
    remarks: str = ""
) -> str:
    """Approve or reject a submitted leave request."""

    action = action.upper()
    request_id = request_id.upper()

    if action not in ["APPROVE", "REJECT"]:
        return "Error: Action must be APPROVE or REJECT."

    with get_db_connection() as conn:

        # Lock the request so two reviewers cannot resolve it concurrently.
        request = conn.execute(
            """
            SELECT *
            FROM leave_requests
            WHERE request_id = %s
            FOR UPDATE
            """,
            (request_id,)
        ).fetchone()

        if not request:
            return f"Error: Leave request '{request_id}' not found."

        if request["status"] != "PENDING":
            return (
                "Error: Request has already been resolved with "
                f"status '{request['status']}'."
            )

        if action == "APPROVE":

            # Lock the employee's balance row.
            balance = conn.execute(
                """
                SELECT available_days
                FROM leave_balances
                WHERE employee_id = %s
                  AND leave_type = %s
                FOR UPDATE
                """,
                (
                    request["employee_id"],
                    request["leave_type"]
                )
            ).fetchone()

            if not balance:
                return "Error: Leave balance record not found."

            if balance["available_days"] < request["days_requested"]:
                return (
                    "Error: Approval failed; employee has "
                    "insufficient remaining balance."
                )

            # Deduct balance only when approval succeeds.
            update_result = conn.execute(
                """
                UPDATE leave_balances
                SET available_days = available_days - %s
                WHERE employee_id = %s
                  AND leave_type = %s
                  AND available_days >= %s
                """,
                (
                    request["days_requested"],
                    request["employee_id"],
                    request["leave_type"],
                    request["days_requested"]
                )
            )

            if update_result.rowcount != 1:
                return "Error: Unable to deduct leave balance."

            new_status = "APPROVED"

        else:
            new_status = "REJECTED"

        conn.execute(
            """
            UPDATE leave_requests
            SET status = %s,
                remarks = %s
            WHERE request_id = %s
            """,
            (new_status, remarks, request_id)
        )

    return (
        f"Request {request_id} has been {new_status}.\n"
        + json.dumps(
            {
                "request_id": request_id,
                "employee_id": request["employee_id"],
                "leave_type": request["leave_type"],
                "days_requested": request["days_requested"],
                "status": new_status,
                "remarks": remarks
            },
            indent=2
        )
    )


# ---------------------------------------------------------
# Tool 4: Get Employee Leave Dates
# ---------------------------------------------------------

@mcp.tool()
def get_employee_leave_dates(
    employee_id: str,
    status: str = "APPROVED",
    month_prefix: Optional[str] = None
) -> str:
    """Retrieve leave dates for an employee."""

    employee_id = employee_id.upper()
    status = status.upper()

    if status not in ["APPROVED", "PENDING", "REJECTED", "ALL"]:
        return "Error: Status must be APPROVED, PENDING, REJECTED, or ALL."

    if month_prefix:
        try:
            if len(month_prefix) == 4:
                datetime.strptime(month_prefix, "%Y")
            elif len(month_prefix) == 7:
                datetime.strptime(month_prefix, "%Y-%m")
            else:
                return "Error: month_prefix must be YYYY or YYYY-MM."
        except ValueError:
            return "Error: Invalid month_prefix."

    with get_db_connection() as conn:

        employee = conn.execute(
            """
            SELECT employee_id, name
            FROM employees
            WHERE employee_id = %s
            """,
            (employee_id,)
        ).fetchone()

        if not employee:
            return f"Error: Employee '{employee_id}' not found."

        query = """
            SELECT
                lr.request_id,
                lr.leave_type,
                lr.status,
                lr.reason,
                lrd.leave_date
            FROM leave_requests lr
            JOIN leave_request_dates lrd
                ON lr.request_id = lrd.request_id
            WHERE lr.employee_id = %s
        """

        params = [employee_id]

        if status != "ALL":
            query += " AND lr.status = %s"
            params.append(status)

        if month_prefix:
            query += " AND TO_CHAR(lrd.leave_date, 'YYYY-MM') LIKE %s"
            params.append(
                month_prefix + "%"
                if len(month_prefix) == 4
                else month_prefix
            )

        query += " ORDER BY lrd.leave_date"

        rows = conn.execute(query, params).fetchall()

    leave_records = {}
    all_dates = set()

    for row in rows:
        request_id = row["request_id"]
        leave_date = row["leave_date"].isoformat()

        all_dates.add(leave_date)

        if request_id not in leave_records:
            leave_records[request_id] = {
                "request_id": request_id,
                "leave_type": row["leave_type"],
                "status": row["status"],
                "reason": row["reason"],
                "dates": []
            }

        leave_records[request_id]["dates"].append(leave_date)

    output = {
        "employee_id": employee["employee_id"],
        "name": employee["name"],
        "total_days_off": len(all_dates),
        "taken_dates": sorted(all_dates),
        "breakdown": list(leave_records.values())
    }

    return json.dumps(output, indent=2)


# ---------------------------------------------------------
# Tool 5: Check Date Availability
# ---------------------------------------------------------

@mcp.tool()
def check_date_availability(target_date: str) -> str:
    """Check employees on approved leave on a specific date."""

    try:
        parsed_date = datetime.strptime(
            target_date, "%Y-%m-%d"
        ).date()
    except ValueError:
        return "Error: Date format must be YYYY-MM-DD."

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                e.employee_id,
                e.name,
                lr.leave_type,
                lr.request_id
            FROM leave_request_dates lrd
            JOIN leave_requests lr
                ON lr.request_id = lrd.request_id
            JOIN employees e
                ON e.employee_id = lr.employee_id
            WHERE lrd.leave_date = %s
              AND lr.status = 'APPROVED'
            ORDER BY e.name
            """,
            (parsed_date,)
        ).fetchall()

    if not rows:
        return f"No employees are on approved leave on {target_date}."

    return json.dumps(
        {
            "date": target_date,
            "employees_on_leave": rows
        },
        indent=2,
        default=str
    )


# ---------------------------------------------------------
# Tool 6: List Leave Requests
# ---------------------------------------------------------

@mcp.tool()
def list_leave_requests(
    status: str = "ALL",
    employee_id: str = ""
) -> str:
    """List leave requests with optional status and employee filters."""

    status = status.upper()

    if status not in ["ALL", "PENDING", "APPROVED", "REJECTED"]:
        return "Error: Invalid status filter."

    query = """
        SELECT
            lr.request_id,
            lr.employee_id,
            e.name AS employee_name,
            lr.leave_type,
            lr.start_date,
            lr.end_date,
            lr.days_requested,
            lr.reason,
            lr.status,
            lr.remarks,
            lr.created_at,
            ARRAY(
                SELECT lrd.leave_date
                FROM leave_request_dates lrd
                WHERE lrd.request_id = lr.request_id
                ORDER BY lrd.leave_date
            ) AS leave_dates
        FROM leave_requests lr
        JOIN employees e
            ON e.employee_id = lr.employee_id
        WHERE 1 = 1
    """

    params = []

    if status != "ALL":
        query += " AND lr.status = %s"
        params.append(status)

    if employee_id:
        query += " AND lr.employee_id = %s"
        params.append(employee_id.upper())

    query += " ORDER BY lr.created_at DESC"

    with get_db_connection() as conn:
        results = conn.execute(query, params).fetchall()

    if not results:
        return "No matching leave requests found."

    return json.dumps(results, indent=2, default=str)


# ---------------------------------------------------------
# Run MCP Server
# ---------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
