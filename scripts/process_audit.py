"""
process_audit.py — Find and optionally kill unnecessary processes.

Usage:
    python process_audit.py           # audit only, exports Excel
    python process_audit.py --kill    # audit + terminate flagged PIDs

Config tweaks:
    MIN_DUPLICATE_COUNT   — flag exe names with >= N instances
    HIGH_MEM_PCT          — flag processes using >= N% RAM
    EXCLUDE_NAMES         — never kill these exe names
"""

import sys
import os
import socket
import argparse
import psutil
import pandas as pd
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MIN_DUPLICATE_COUNT = 3     # flag if same exe has this many or more instances
HIGH_MEM_PCT = 5.0          # flag if single process uses >= this % RAM
EXCLUDE_NAMES = {           # never touch these even in kill mode
    "System", "Registry", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "explorer.exe", "taskmgr.exe", "python.exe",   # remove python.exe to allow killing
}
EXCLUDE_SYSTEM_USERS = {    # never kill processes owned by these accounts
    "", "SYSTEM", "NT AUTHORITY\\SYSTEM", "NT AUTHORITY\\LOCAL SERVICE",
    "NT AUTHORITY\\NETWORK SERVICE",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def bytes_to_mb(value):
    try:
        return round(value / (1024 * 1024), 2)
    except Exception:
        return None


def get_script_name(cmdline_list):
    for token in (cmdline_list or [])[1:]:
        if token.endswith((".py", ".js", ".ts", ".rb", ".php")):
            return os.path.basename(token)
    return ""


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------
def collect_processes():
    print("Collecting process information...")
    rows = []

    for proc in psutil.process_iter([
        "pid", "ppid", "name", "username", "status",
        "cpu_percent", "memory_percent", "create_time", "exe", "cmdline",
    ]):
        try:
            info = proc.info
            pid = info["pid"]
            name = info.get("name") or ""
            cmdline_list = info.get("cmdline") or []
            cmdline = " ".join(cmdline_list)

            create_time = info.get("create_time")
            started = (
                datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S")
                if create_time else ""
            )

            try:
                mem = proc.memory_info()
                rss_mb = bytes_to_mb(mem.rss)
                vms_mb = bytes_to_mb(mem.vms)
            except Exception:
                rss_mb = vms_mb = None

            try:
                threads = proc.num_threads()
            except Exception:
                threads = None

            try:
                connections = len(proc.net_connections())
            except Exception:
                connections = None

            try:
                open_files = len(proc.open_files())
            except Exception:
                open_files = None

            rows.append({
                "PID":            pid,
                "PPID":           info.get("ppid"),
                "Process Name":   name,
                "Script":         get_script_name(cmdline_list),
                "Username":       info.get("username") or "",
                "Status":         info.get("status") or "",
                "CPU %":          info.get("cpu_percent", 0),
                "Memory %":       round(info.get("memory_percent") or 0, 2),
                "RSS MB":         rss_mb,
                "VMS MB":         vms_mb,
                "Threads":        threads,
                "Connections":    connections,
                "Open Files":     open_files,
                "Started":        started,
                "Executable":     info.get("exe") or "",
                "Command Line":   cmdline,
            })

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyse(df):
    name_lower = df["Process Name"].str.lower()
    name_counts = name_lower.value_counts()
    df["Instance Count"] = name_lower.map(name_counts)

    def flag(row):
        reasons = []
        if row["Status"] in ("zombie", "stopped"):
            reasons.append(f"status={row['Status']}")
        if row["Instance Count"] >= MIN_DUPLICATE_COUNT:
            reasons.append(f"{int(row['Instance Count'])}x duplicate")
        if row["Memory %"] >= HIGH_MEM_PCT:
            reasons.append(f"high-mem {row['Memory %']}%")
        return " | ".join(reasons)

    df["Flag Reason"] = df.apply(flag, axis=1)
    df["Flagged"] = df["Flag Reason"].ne("")
    return df


def build_duplicates_sheet(df):
    dupes = (
        df.groupby("Process Name", as_index=False)
        .agg(
            Count=("PID", "count"),
            Total_RSS_MB=("RSS MB", "sum"),
            Total_Mem_Pct=("Memory %", "sum"),
            PIDs=("PID", lambda s: ", ".join(str(x) for x in s)),
            Sample_EXE=("Executable", "first"),
            Sample_CMD=("Command Line", "first"),
        )
        .sort_values("Count", ascending=False)
    )
    return dupes[dupes["Count"] >= 2].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------
def kill_flagged(df_flagged):
    candidates = df_flagged[
        ~df_flagged["Process Name"].isin(EXCLUDE_NAMES) &
        ~df_flagged["Username"].isin(EXCLUDE_SYSTEM_USERS)
    ]

    if candidates.empty:
        print("\nNo killable candidates after exclusions.")
        return

    print(f"\n{'='*60}")
    print(f"KILL MODE — {len(candidates)} candidate(s):")
    print(candidates[["PID", "Process Name", "Username", "Memory %", "Flag Reason"]].to_string(index=False))
    print(f"{'='*60}")
    confirm = input("Terminate these? [yes/no]: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return

    killed = failed = 0
    for _, row in candidates.iterrows():
        try:
            p = psutil.Process(int(row["PID"]))
            p.terminate()
            killed += 1
            print(f"  Terminated  PID {row['PID']:>6}  {row['Process Name']}")
        except Exception as e:
            failed += 1
            print(f"  FAILED      PID {row['PID']:>6}  {row['Process Name']}  — {e}")

    print(f"\nTerminated: {killed}   Failed: {failed}")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export(df_all, df_dupes, df_flagged):
    hostname = socket.gethostname()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(os.path.dirname(__file__), f"process_audit_{hostname}_{ts}.xlsx")

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df_all.drop(columns=["Flagged"]).to_excel(writer, index=False, sheet_name="All Processes")
        df_dupes.to_excel(writer, index=False, sheet_name="Duplicates")
        df_flagged.drop(columns=["Flagged"]).to_excel(writer, index=False, sheet_name="Flagged - Review")

    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Process audit tool")
    parser.add_argument("--kill", action="store_true", help="Terminate flagged processes after review")
    args = parser.parse_args()

    df = collect_processes()
    df = analyse(df)

    df_all = df.sort_values(["Flagged", "Memory %"], ascending=[False, False])
    df_dupes = build_duplicates_sheet(df)
    df_flagged = df[df["Flagged"]].sort_values("Memory %", ascending=False)

    report_path = export(df_all, df_dupes, df_flagged)

    print(f"\nReport : {report_path}")
    print(f"Total  : {len(df)} processes")
    print(f"Flagged: {df['Flagged'].sum()} (see 'Flagged - Review' sheet)")

    print("\nTop duplicate processes:")
    print(df_dupes.head(20)[["Process Name", "Count", "Total_RSS_MB", "Total_Mem_Pct", "PIDs"]].to_string(index=False))

    if args.kill:
        kill_flagged(df_flagged)
    else:
        print("\nRun with --kill to terminate flagged processes after reviewing the report.")


if __name__ == "__main__":
    main()
