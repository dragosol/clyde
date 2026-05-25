"""
macos_bridge.py — AppleScript-based automation for macOS system apps.

Each function builds an AppleScript, executes it via `osascript`, and
returns a structured result (JSON string or plain text).

Used by the iCloud/macOS tools registered in tools.py.  All write/send
operations expect the MODEL to have already called ask_user for
confirmation before invoking these functions.

Supported apps: Messages, Calendar, Contacts, Mail, Notes, Reminders.
"""

import json
import logging
import subprocess
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("macos_bridge")

# ── Helpers ──────────────────────────────────────────────────────────

def _run_osascript(script: str, timeout: int = 15) -> tuple[bool, str]:
    """Run AppleScript and return (success, output_or_error)."""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            return True, r.stdout.strip()
        else:
            err = (r.stderr or r.stdout or "").strip()
            log.warning(f"[macOS] osascript failed: {err}")
            return False, err
    except subprocess.TimeoutExpired:
        return False, "AppleScript timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def _parse_date(s: str) -> Optional[str]:
    """Best-effort parse of natural-language or ISO date into
    AppleScript-friendly format: 'April 16, 2026 at 2:00 PM'."""
    if not s:
        return None
    # Already looks like a formatted date
    s = s.strip()
    # Try ISO 8601
    for fmt in (
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%B %d, %Y at %I:%M %p")
        except ValueError:
            continue
    # Relative dates
    today = datetime.now()
    low = s.lower()
    if low == "today":
        return today.strftime("%B %d, %Y at 9:00 AM")
    if low == "tomorrow":
        return (today + timedelta(days=1)).strftime("%B %d, %Y at 9:00 AM")
    # Pass through — let AppleScript try
    return s


def _escape(s: str) -> str:
    """Escape a string for embedding in AppleScript double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Messages ─────────────────────────────────────────────────────────

def messages_send(recipient: str, text: str, attachment: str | None = None) -> str:
    """Send an iMessage or SMS.  recipient can be phone or email.
    Optionally attach a file by providing its absolute path."""

    # Build the send commands — text first, then attachment if provided
    send_cmds = f'send "{_escape(text)}" to targetBuddy'
    if attachment:
        import shutil
        # Messages.app is sandboxed and CANNOT read from /tmp or hidden dirs.
        # Copy to ~/Pictures/ClydeAttachments/ which Messages can access.
        safe_dir = Path.home() / "Pictures" / "ClydeAttachments"
        safe_dir.mkdir(parents=True, exist_ok=True)
        att_path = Path(attachment)
        if att_path.exists():
            safe_path = safe_dir / att_path.name
            if str(att_path) != str(safe_path):
                shutil.copy2(str(att_path), str(safe_path))
                log.info(f"Copied attachment to Messages-safe path: {safe_path}")
            attachment = str(safe_path)
        send_cmds += f'\n    send POSIX file "{_escape(attachment)}" to targetBuddy'

    script = f'''
tell application "Messages"
    set targetService to 1st account whose service type = iMessage
    set targetBuddy to participant "{_escape(recipient)}" of targetService
    {send_cmds}
end tell
'''
    ok, out = _run_osascript(script)
    if ok:
        msg = f"Message sent to {recipient}"
        if attachment:
            msg += f" with attachment {attachment.split('/')[-1]}"
        return json.dumps({"ok": True, "message": msg})
    # Fallback: try SMS service (no attachment — SMS doesn't support files)
    script2 = f'''
tell application "Messages"
    set targetService to 1st account whose service type = SMS
    set targetBuddy to participant "{_escape(recipient)}" of targetService
    send "{_escape(text)}" to targetBuddy
end tell
'''
    ok2, out2 = _run_osascript(script2)
    if ok2:
        msg = f"SMS sent to {recipient}"
        if attachment:
            msg += " (attachment not supported via SMS — text only)"
        return json.dumps({"ok": True, "message": msg})
    return f"ERROR: Could not send message — {out}. Fallback SMS also failed: {out2}"


def messages_read(contact: str, count: int = 20) -> str:
    """Read recent messages from a conversation using the Messages SQLite DB.

    The AppleScript `messages of chat` API is unreliable (type coercion
    errors), so we read directly from ~/Library/Messages/chat.db which
    is the canonical source. Requires Full Disk Access for the Python
    process, or at minimum Files & Folders access to ~/Library/Messages.
    """
    import sqlite3
    from datetime import datetime, timezone, timedelta

    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        return f"ERROR: Messages database not found at {db_path}"

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Find chat(s) matching the contact name or handle
        cur.execute("""
            SELECT DISTINCT cmj.chat_id
            FROM chat_message_join cmj
            JOIN chat c ON c.ROWID = cmj.chat_id
            JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
            JOIN handle h ON h.ROWID = chj.handle_id
            WHERE h.id LIKE ? OR h.id LIKE ?
            LIMIT 1
        """, (f"%{contact}%", f"%{contact}%"))
        row = cur.fetchone()

        if not row:
            # Try matching by display_name on the chat itself
            cur.execute("""
                SELECT ROWID FROM chat
                WHERE display_name LIKE ?
                LIMIT 1
            """, (f"%{contact}%",))
            row = cur.fetchone()
            if not row:
                conn.close()
                return f"ERROR: No conversation found matching '{contact}'"
            chat_id = row[0]
        else:
            chat_id = row["chat_id"]

        # Read recent messages from that chat
        # Apple epoch: 2001-01-01 00:00:00 UTC (978307200 seconds after Unix epoch)
        cur.execute(f"""
            SELECT
                m.text,
                m.date / 1000000000 + 978307200 AS unix_ts,
                m.is_from_me,
                COALESCE(h.id, 'Me') AS sender_handle
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE cmj.chat_id = ?
            ORDER BY m.date DESC
            LIMIT ?
        """, (chat_id, count))

        rows = cur.fetchall()
        conn.close()

        messages = []
        for r in reversed(rows):  # chronological order
            text = r["text"] or "(attachment/reaction)"
            ts = r["unix_ts"]
            try:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
                date_str = dt.strftime("%Y-%m-%d %I:%M %p")
            except Exception:
                date_str = str(ts)
            sender = "You" if r["is_from_me"] else r["sender_handle"]
            messages.append({"sender": sender, "date": date_str, "text": text})

        return json.dumps({"contact": contact, "count": len(messages), "messages": messages})

    except sqlite3.OperationalError as e:
        if "unable to open" in str(e).lower() or "permission" in str(e).lower():
            return (
                f"ERROR: Cannot access Messages database — Full Disk Access is required. "
                f"Tell the user to open Clyde Settings → Permissions and grant Full Disk Access, "
                f"then restart Clyde."
            )
        return f"ERROR: SQLite error reading Messages — {e}"
    except Exception as e:
        return f"ERROR: Failed to read messages — {e}"


def messages_list_chats(count: int = 15) -> str:
    """List recent conversations with last message preview."""
    script = f'''
tell application "Messages"
    set output to ""
    set chatList to chats
    set limit to {count}
    if (count of chatList) < limit then set limit to count of chatList
    repeat with i from 1 to limit
        set c to item i of chatList
        set chatName to name of c
        if chatName is missing value then set chatName to id of c
        set lastMsg to ""
        set lastDate to ""
        try
            set msgs to messages of c
            if (count of msgs) > 0 then
                set lastM to last item of msgs
                set lastMsg to text of lastM
                set lastDate to date sent of lastM as string
            end if
        end try
        if length of lastMsg > 100 then set lastMsg to text 1 thru 100 of lastMsg & "..."
        set output to output & chatName & " ||| " & lastDate & " ||| " & lastMsg & linefeed
    end repeat
    return output
end tell
'''
    ok, out = _run_osascript(script, timeout=30)
    if not ok:
        return f"ERROR: Could not list chats — {out}"
    chats = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ||| ", 2)
        if len(parts) == 3:
            chats.append({"name": parts[0], "last_date": parts[1], "last_message": parts[2]})
        else:
            chats.append({"raw": line})
    return json.dumps({"chats": chats, "count": len(chats)})


# ── Calendar ─────────────────────────────────────────────────────────

def calendar_create_event(
    title: str,
    start_date: str,
    end_date: str = "",
    calendar_name: str = "",
    location: str = "",
    notes: str = "",
    all_day: bool = False,
) -> str:
    """Create a calendar event."""
    start_as = _parse_date(start_date) or start_date
    end_as = _parse_date(end_date) if end_date else None

    # If no end date, default to 1 hour after start
    if not end_as:
        end_as_line = 'set endD to startD + (1 * hours)'
    else:
        end_as_line = f'set endD to date "{_escape(end_as)}"'

    cal_line = ""
    if calendar_name:
        cal_line = f'set targetCal to calendar "{_escape(calendar_name)}"'
    else:
        cal_line = 'set targetCal to first calendar whose name is not ""'

    allday_line = f"set allday event of newEvent to true" if all_day else ""
    loc_line = f'set location of newEvent to "{_escape(location)}"' if location else ""
    notes_line = f'set description of newEvent to "{_escape(notes)}"' if notes else ""

    script = f'''
tell application "Calendar"
    {cal_line}
    set startD to date "{_escape(start_as)}"
    {end_as_line}
    set newEvent to make new event at end of events of targetCal with properties {{summary:"{_escape(title)}", start date:startD, end date:endD}}
    {allday_line}
    {loc_line}
    {notes_line}
    return "OK"
end tell
'''
    ok, out = _run_osascript(script)
    if ok:
        return json.dumps({"ok": True, "event": title, "start": start_as, "calendar": calendar_name or "(default)"})
    return f"ERROR: Could not create event — {out}"


def calendar_list_events(days_ahead: int = 7, calendar_name: str = "") -> str:
    """List upcoming events."""
    cal_filter = ""
    if calendar_name:
        cal_filter = f'whose name is "{_escape(calendar_name)}"'

    script = f'''
tell application "Calendar"
    set today to current date
    set endRange to today + ({days_ahead} * days)
    set output to ""
    repeat with cal in (calendars {cal_filter})
        set calName to name of cal
        repeat with e in (events of cal whose start date >= today and start date <= endRange)
            set evtTitle to summary of e
            set evtStart to start date of e as string
            set evtEnd to end date of e as string
            set evtLoc to ""
            try
                set evtLoc to location of e
            end try
            set output to output & calName & " ||| " & evtTitle & " ||| " & evtStart & " ||| " & evtEnd & " ||| " & evtLoc & linefeed
        end repeat
    end repeat
    return output
end tell
'''
    ok, out = _run_osascript(script, timeout=30)
    if not ok:
        return f"ERROR: Could not list events — {out}"
    events = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ||| ")
        if len(parts) >= 4:
            events.append({
                "calendar": parts[0],
                "title": parts[1],
                "start": parts[2],
                "end": parts[3],
                "location": parts[4] if len(parts) > 4 else "",
            })
    return json.dumps({"events": events, "count": len(events), "days_ahead": days_ahead})


def calendar_list_calendars() -> str:
    """List all available calendars."""
    script = '''
tell application "Calendar"
    set output to ""
    repeat with cal in calendars
        set calName to name of cal
        set output to output & calName & linefeed
    end repeat
    return output
end tell
'''
    ok, out = _run_osascript(script)
    if not ok:
        return f"ERROR: Could not list calendars — {out}"
    cals = [c.strip() for c in out.split("\n") if c.strip()]
    return json.dumps({"calendars": cals, "count": len(cals)})


# ── Contacts ─────────────────────────────────────────────────────────

def contacts_search(query: str) -> str:
    """Search contacts by name, email, or phone."""
    # Ensure Contacts.app is running in the background — use `open -gj` to
    # launch without stealing focus, then poll until responsive.
    import subprocess as _sp, time as _t
    _sp.run(["open", "-gj", "-a", "Contacts"], timeout=5)
    for _ in range(30):
        _ok, _ = _run_osascript('tell application "Contacts" to count people', timeout=5)
        if _ok:
            break
        _t.sleep(0.3)

    script = f'''
tell application "Contacts"
    set output to ""
    set matches to (every person whose name contains "{_escape(query)}")
    -- Also search by email and phone
    set emailMatches to (every person whose value of emails contains "{_escape(query)}")
    set phoneMatches to (every person whose value of phones contains "{_escape(query)}")

    -- Combine (may have dupes, that's ok for display)
    set allMatches to matches & emailMatches & phoneMatches
    set seen to {{}}

    repeat with p in allMatches
        set pName to name of p
        if pName is not in seen then
            set end of seen to pName
            set pPhones to ""
            try
                repeat with ph in phones of p
                    set pPhones to pPhones & value of ph & ", "
                end repeat
            end try
            set pEmails to ""
            try
                repeat with em in emails of p
                    set pEmails to pEmails & value of em & ", "
                end repeat
            end try
            set pCompany to ""
            try
                set pCompany to organization of p
            end try
            set output to output & pName & " ||| " & pPhones & " ||| " & pEmails & " ||| " & pCompany & linefeed
        end if
    end repeat
    return output
end tell
'''
    ok, out = _run_osascript(script, timeout=20)
    if not ok:
        return f"ERROR: Could not search contacts — {out}"
    contacts = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ||| ")
        if len(parts) >= 3:
            contacts.append({
                "name": parts[0].strip(),
                "phones": [p.strip() for p in parts[1].split(",") if p.strip()],
                "emails": [e.strip() for e in parts[2].split(",") if e.strip()],
                "company": parts[3].strip() if len(parts) > 3 else "",
            })
    return json.dumps({"query": query, "results": contacts, "count": len(contacts)})


def contacts_create(
    first_name: str, last_name: str = "",
    phone: str = "", email: str = "", company: str = "",
) -> str:
    """Create a new contact."""
    props = [f'first name:"{_escape(first_name)}"']
    if last_name:
        props.append(f'last name:"{_escape(last_name)}"')
    if company:
        props.append(f'organization:"{_escape(company)}"')

    phone_line = ""
    if phone:
        phone_line = f'make new phone at end of phones of newPerson with properties {{value:"{_escape(phone)}", label:"mobile"}}'
    email_line = ""
    if email:
        email_line = f'make new email at end of emails of newPerson with properties {{value:"{_escape(email)}", label:"home"}}'

    script = f'''
tell application "Contacts"
    set newPerson to make new person with properties {{{", ".join(props)}}}
    {phone_line}
    {email_line}
    save
    return name of newPerson
end tell
'''
    ok, out = _run_osascript(script)
    if ok:
        return json.dumps({"ok": True, "contact": out})
    return f"ERROR: Could not create contact — {out}"


# ── Mail ─────────────────────────────────────────────────────────────

def mail_send(
    to: str, subject: str, body: str,
    cc: str = "", attachment_path: str = "",
) -> str:
    """Send an email via Mail.app."""
    cc_line = ""
    if cc:
        for addr in cc.split(","):
            addr = addr.strip()
            if addr:
                cc_line += f'\nmake new cc recipient at end of cc recipients of newMsg with properties {{address:"{_escape(addr)}"}}'

    attach_line = ""
    if attachment_path:
        attach_line = f'make new attachment with properties {{file name:(POSIX file "{_escape(attachment_path)}")}} at after the last paragraph of content of newMsg'

    script = f'''
tell application "Mail"
    set newMsg to make new outgoing message with properties {{subject:"{_escape(subject)}", content:"{_escape(body)}", visible:false}}
    make new to recipient at end of to recipients of newMsg with properties {{address:"{_escape(to)}"}}
    {cc_line}
    {attach_line}
    send newMsg
    return "OK"
end tell
'''
    ok, out = _run_osascript(script, timeout=20)
    if ok:
        return json.dumps({"ok": True, "to": to, "subject": subject})
    return f"ERROR: Could not send email — {out}"


def mail_read(mailbox: str = "INBOX", count: int = 10, account: str = "") -> str:
    """Read recent emails."""
    acct_part = ""
    if account:
        acct_part = f'of account "{_escape(account)}"'

    script = f'''
tell application "Mail"
    set output to ""
    set msgs to messages of mailbox "{_escape(mailbox)}" {acct_part}
    set limit to {count}
    if (count of msgs) < limit then set limit to count of msgs
    repeat with i from 1 to limit
        set m to item i of msgs
        set subj to subject of m
        set sndr to sender of m
        set dt to date received of m as string
        set snip to ""
        try
            set snip to text 1 thru 200 of (content of m as string)
        on error
            try
                set snip to content of m as string
            end try
        end try
        set output to output & sndr & " ||| " & subj & " ||| " & dt & " ||| " & snip & linefeed
    end repeat
    return output
end tell
'''
    ok, out = _run_osascript(script, timeout=30)
    if not ok:
        return f"ERROR: Could not read mail — {out}"
    emails = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ||| ", 3)
        if len(parts) >= 3:
            emails.append({
                "from": parts[0],
                "subject": parts[1],
                "date": parts[2],
                "snippet": parts[3] if len(parts) > 3 else "",
            })
    return json.dumps({"mailbox": mailbox, "emails": emails, "count": len(emails)})


def mail_search(query: str, mailbox: str = "", count: int = 10) -> str:
    """Search mail by keyword in subject or content."""
    mbox_part = ""
    if mailbox:
        mbox_part = f'of mailbox "{_escape(mailbox)}"'

    # Search by subject first (fastest), then broader
    script = f'''
tell application "Mail"
    set output to ""
    set limit to {count}
    set found to 0
    -- Search across all accounts
    repeat with acct in accounts
        repeat with mbox in mailboxes of acct
            if found >= limit then exit repeat
            try
                set msgs to (messages of mbox whose subject contains "{_escape(query)}")
                repeat with m in msgs
                    if found >= limit then exit repeat
                    set subj to subject of m
                    set sndr to sender of m
                    set dt to date received of m as string
                    set mboxName to name of mbox
                    set output to output & sndr & " ||| " & subj & " ||| " & dt & " ||| " & mboxName & linefeed
                    set found to found + 1
                end repeat
            end try
        end repeat
        if found >= limit then exit repeat
    end repeat
    return output
end tell
'''
    ok, out = _run_osascript(script, timeout=45)
    if not ok:
        return f"ERROR: Could not search mail — {out}"
    emails = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ||| ", 3)
        if len(parts) >= 3:
            emails.append({
                "from": parts[0],
                "subject": parts[1],
                "date": parts[2],
                "mailbox": parts[3] if len(parts) > 3 else "",
            })
    return json.dumps({"query": query, "results": emails, "count": len(emails)})


# ── Notes ────────────────────────────────────────────────────────────

def notes_create(title: str, body: str, folder: str = "") -> str:
    """Create a note in Notes.app."""
    # Notes uses HTML body
    html_body = body.replace("\n", "<br>")
    folder_part = ""
    if folder:
        folder_part = f'in folder "{_escape(folder)}"'
    else:
        folder_part = 'in default account'

    script = f'''
tell application "Notes"
    set newNote to make new note {folder_part} with properties {{name:"{_escape(title)}", body:"{_escape(html_body)}"}}
    return name of newNote
end tell
'''
    ok, out = _run_osascript(script)
    if ok:
        return json.dumps({"ok": True, "title": out})
    return f"ERROR: Could not create note — {out}"


def notes_search(query: str, count: int = 10) -> str:
    """Search notes by keyword."""
    script = f'''
tell application "Notes"
    set output to ""
    set found to 0
    set limit to {count}
    repeat with n in notes
        if found >= limit then exit repeat
        set nName to name of n
        set nBody to ""
        try
            set nBody to plaintext of n
        end try
        if nName contains "{_escape(query)}" or nBody contains "{_escape(query)}" then
            set modDate to modification date of n as string
            set snippet to ""
            if length of nBody > 200 then
                set snippet to text 1 thru 200 of nBody
            else
                set snippet to nBody
            end if
            set output to output & nName & " ||| " & modDate & " ||| " & snippet & linefeed
            set found to found + 1
        end if
    end repeat
    return output
end tell
'''
    ok, out = _run_osascript(script, timeout=30)
    if not ok:
        return f"ERROR: Could not search notes — {out}"
    notes = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ||| ", 2)
        if len(parts) >= 2:
            notes.append({
                "title": parts[0],
                "modified": parts[1],
                "snippet": parts[2] if len(parts) > 2 else "",
            })
    return json.dumps({"query": query, "results": notes, "count": len(notes)})


# ── Reminders ────────────────────────────────────────────────────────

def reminders_create(
    title: str,
    due_date: str = "",
    list_name: str = "",
    notes: str = "",
    priority: str = "none",
) -> str:
    """Create a reminder."""
    due_line = ""
    if due_date:
        parsed = _parse_date(due_date)
        if parsed:
            due_line = f'set due date of newReminder to date "{_escape(parsed)}"'

    list_part = ""
    if list_name:
        list_part = f'of list "{_escape(list_name)}"'
    else:
        list_part = 'of default list'

    priority_map = {"none": 0, "low": 9, "medium": 5, "high": 1}
    p_val = priority_map.get(priority.lower(), 0)
    priority_line = f"set priority of newReminder to {p_val}" if p_val > 0 else ""
    notes_line = f'set body of newReminder to "{_escape(notes)}"' if notes else ""

    script = f'''
tell application "Reminders"
    set newReminder to make new reminder {list_part} with properties {{name:"{_escape(title)}"}}
    {due_line}
    {priority_line}
    {notes_line}
    return name of newReminder
end tell
'''
    ok, out = _run_osascript(script)
    if ok:
        return json.dumps({"ok": True, "reminder": out, "list": list_name or "(default)"})
    return f"ERROR: Could not create reminder — {out}"
