#!/usr/bin/env python3
# shredder_touch_buttons.py
# ✅ Bottom touchable char buttons + full mouse/touch support + directory nav shredder
# Feature: Dual‑mode shred: system shred(with -v real‑time progress) / python internal overwrite
# Hotkey: / search, n=next match, N=prev match, Alt‑W/Q also supported; : command‑mode cd / cd‑
import curses
import os
import secrets
import sys
import subprocess
import re
from pathlib import Path

PAT_PASS = re.compile(r"pass\s+(\d+)/(\d+)")

def shred_single_file_python(file_path: str, passes: int = 2, chunk_size: int = 1024*1024):
    if not os.path.isfile(file_path):
        yield (0, "skip:not a file")
        return
    try:
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            os.remove(file_path)
            yield (100, "OK:empty removed")
            return
        with open(file_path, "r+b") as f:
            for pass_no in range(passes):
                f.seek(0)
                written = 0
                while written < file_size:
                    to_write = min(chunk_size, file_size - written)
                    data = secrets.token_bytes(to_write)
                    f.write(data)
                    written += to_write
                    pct = int(written / file_size * 100)
                    yield (pct, f"Pass {pass_no+1}/{passes} writing")
                f.flush()
                os.fsync(f.fileno())
        os.remove(file_path)
        yield (100, "✅ File shredded & deleted")
    except PermissionError:
        yield (-1, "❌ Permission denied")
    except Exception as e:
        yield (-1, f"❌ Error:{str(e)}")

def shred_single_file_system(file_path: str, passes: int = 2, chunk_size: int = 1024*1024):
    if not os.path.isfile(file_path):
        yield (0, "skip:not a file")
        return
    try:
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            os.remove(file_path)
            yield (100, "OK:empty removed")
            return

        proc = subprocess.Popen(
            ["shred", f"-n{passes}", "-u", "-z", "-v", file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            text=True
        )

        yield (0, f"Start shred pass={passes} (+zero pass)")
        while proc.poll() is None:
            line = proc.stderr.readline()
            if not line:
                continue
            m = PAT_PASS.search(line)
            if m:
                p_no = int(m.group(1))
                p_total = int(m.group(2))
                pct = int((p_no / p_total) * 100)
                yield (pct, f"Pass {p_no}/{p_total} writing")

        ret = proc.wait()
        proc.stderr.read()
        proc.stdout.read()

        if ret == 0:
            yield (100, "✅ File shredded & deleted")
        else:
            yield (-1, f"❌ shred exited code {ret}")

    except FileNotFoundError:
        yield (-1, "❌ ERROR: shred not found, run pkg install coreutils")
    except PermissionError:
        yield (-1, "❌ Permission denied")
    except Exception as e:
        yield (-1, f"❌ Error:{str(e)}")

def shred_single_file(file_path: str, passes: int = 2, chunk_size: int = 1024*1024):
    if USE_SYSTEM_SHRED:
        yield from shred_single_file_system(file_path, passes, chunk_size)
    else:
        yield from shred_single_file_python(file_path, passes, chunk_size)

def collect_all_files(target_path: str):
    out = []
    p = Path(target_path)
    if p.is_file():
        return [str(p)]
    elif p.is_dir():
        for root, dirs, files in os.walk(p):
            for fn in files:
                fp = os.path.join(root, fn)
                out.append(fp)
    return out

def tui_main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    # ===================== 配置区 =====================
    global USE_SYSTEM_SHRED
    SHRED_PASSES = 2
    USE_SYSTEM_SHRED = True   # True=系统shred命令；False=Python内部覆写
    current_pwd = os.path.abspath("/storage/emulated/0/Download/shred_test")
    old_pwd = current_pwd  # for cd -
    # ==================================================
    original_scan_entries = []
    scan_entries = []
    cursor_idx = 0
    selected_set = set()
    view_offset = 0

    # 搜索相关
    search_mode = False
    search_keyword = ""
    search_matches = []
    current_match_idx = 0

    MODE_LIST = "list"
    MODE_PREVIEW = "preview"
    MODE_CONFIRM1 = "confirm1"
    MODE_CONFIRM2 = "confirm2"
    MODE_SHREDDING = "shredding"
    MODE_CMDLINE = "cmdline"
    mode = MODE_LIST

    cmd_buffer = ""
    cmd_error = ""

    all_selected_entries = []
    total_file_list = []
    total_file_count = 0
    current_global_idx = 0
    current_file_generator = None
    current_file_status = ""
    current_file_pct = 0

    btn_defs = [
        ("[Quit]", 0, 7, "quit"),
        ("[SelectAll]", 9, 21, "sel_all"),
        ("[ClearSel]", 23, 35, "clear_sel"),
        ("[Confirm]", 37, 48, "confirm"),
    ]

    def rescan_directory():
        nonlocal original_scan_entries, scan_entries, cursor_idx, view_offset
        original_scan_entries.clear()
        parent_path = os.path.dirname(current_pwd.rstrip("/"))
        original_scan_entries.append({
            "path": parent_path,
            "name": ".. (GO UP)",
            "is_dir": True,
            "is_parent_link": True
        })
        try:
            for entry in os.listdir(current_pwd):
                fullp = os.path.join(current_pwd, entry)
                original_scan_entries.append({
                    "path": fullp,
                    "name": entry,
                    "is_dir": os.path.isdir(fullp),
                    "is_parent_link": False
                })
        except PermissionError:
            pass
        scan_entries = original_scan_entries.copy()
        cursor_idx = 0
        view_offset = 0
        selected_set.clear()

    def compute_search_matches(kw: str):
        matches = []
        kw_low = kw.lower()
        if not kw_low:
            return matches
        for idx, ent in enumerate(original_scan_entries):
            if kw_low in ent["name"].lower():
                matches.append(idx)
        return matches

    def goto_next_match():
        nonlocal cursor_idx, view_offset, current_match_idx
        if not search_matches:
            return
        current_match_idx += 1
        if current_match_idx >= len(search_matches):
            current_match_idx = 0
        target_idx = search_matches[current_match_idx]
        cursor_idx = target_idx
        h, _ = stdscr.getmaxyx()
        max_lines = (h - 1) - 4
        if cursor_idx < view_offset:
            view_offset = cursor_idx
        if cursor_idx >= view_offset + max_lines:
            view_offset = cursor_idx - max_lines + 1

    def goto_prev_match():
        nonlocal cursor_idx, view_offset, current_match_idx
        if not search_matches:
            return
        current_match_idx -= 1
        if current_match_idx < 0:
            current_match_idx = len(search_matches) -1
        target_idx = search_matches[current_match_idx]
        cursor_idx = target_idx
        h, _ = stdscr.getmaxyx()
        max_lines = (h -1) -4
        if cursor_idx < view_offset:
            view_offset = cursor_idx
        if cursor_idx >= view_offset + max_lines:
            view_offset = cursor_idx - max_lines +1

    rescan_directory()

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        btn_row = h - 1

        if mode == MODE_LIST:
            if search_mode:
                title = f"🔍 SEARCH | PWD: {current_pwd} | ENTER=confirm,ESC=cancel"
            else:
                title = f"📂 FileShredder | /搜索 :命令 n=下匹配 N=上匹配 Alt‑W/Q也可用"
            stdscr.addstr(0, 0, title[:w-1], curses.A_BOLD)
            stdscr.addstr(1, 0, "-" * min(w, len(title)))

            max_lines = btn_row - 4
            if cursor_idx < view_offset:
                view_offset = cursor_idx
            if cursor_idx >= view_offset + max_lines:
                view_offset = cursor_idx - max_lines +1

            for disp_i in range(max_lines):
                real_i = view_offset + disp_i
                if real_i >= len(scan_entries):
                    break
                ent = scan_entries[real_i]
                real_original_idx = original_scan_entries.index(ent)
                marker = "[✓]" if real_original_idx in selected_set else "[ ]"
                if ent.get("is_parent_link", False):
                    icon = "⬆️"
                elif ent["is_dir"]:
                    icon = "📁"
                else:
                    icon = "📄"
                line_text = f"{marker} {icon} {ent['name']}"
                attr = curses.A_REVERSE if real_i == cursor_idx else curses.A_NORMAL
                stdscr.addstr(2 + disp_i, 0, line_text[:w-1], attr)

            status_line_y = btn_row -1
            if search_mode:
                search_line = f"/{search_keyword}"
                stdscr.addstr(status_line_y,0, search_line[:w-1], curses.A_REVERSE)
            else:
                match_cnt = len(search_matches)
                status = f"↑↓滚动 | 点击勾选 | Sel:{len(selected_set)} | Matches:{match_cnt}"
                stdscr.addstr(status_line_y,0, status[:w-1])

            btn_text = "  ".join([b[0] for b in btn_defs])
            stdscr.addstr(btn_row, 0, btn_text[:w-1], curses.A_BOLD | curses.A_REVERSE)

        elif mode == MODE_PREVIEW:
            stdscr.addstr(0, 0, "📋 Preview selected items (Folders will RECURSIVELY shred)", curses.A_BOLD)
            stdscr.addstr(1, 0, "=" * (w-1))
            y = 2
            for idx, ent in enumerate(all_selected_entries):
                if ent.get("is_parent_link", False):
                    continue
                typ = "FOLDER(RECURSIVE)" if ent["is_dir"] else "FILE"
                line = f"[{idx+1}] {typ}: {ent['path']}"
                if y >= btn_row -1:
                    break
                stdscr.addstr(y, 0, line[:w-1])
                y += 1
            stdscr.addstr(btn_row -1,0,"ENTER → Confirm | ESC Back")
            btn_text = "[Quit]  [Back]"
            stdscr.addstr(btn_row,0, btn_text[:w-1], curses.A_BOLD | curses.A_REVERSE)

        elif mode == MODE_CONFIRM1:
            msg = "⚠️ FIRST CONFIRM: You are going to shred selected items!"
            warn = "Folders will recursively overwrite ALL inner files!"
            prompt = "Press ENTER to continue to SECOND confirmation, ESC cancel"
            stdscr.addstr(h//2 - 2, max(0, w//2 - len(msg)//2), msg, curses.A_BOLD | curses.A_REVERSE)
            stdscr.addstr(h//2 - 1, max(0, w//2 - len(warn)//2), warn)
            stdscr.addstr(h//2, max(0, w//2 - len(prompt)//2), prompt)
            btn_text = "[Quit]  [Cancel]  [NextConfirm]"
            stdscr.addstr(btn_row,0, btn_text[:w-1], curses.A_BOLD | curses.A_REVERSE)

        elif mode == MODE_CONFIRM2:
            msg = "🚨 FINAL CONFIRM: DATA CANNOT BE RESTORED AFTER SHREDDING!"
            prompt = ">> ENTER START SHREDDING | ESC ABORT <<"
            stdscr.addstr(h//2 - 1, max(0, w//2 - len(msg)//2), msg, curses.A_BOLD | curses.A_REVERSE)
            stdscr.addstr(h//2, max(0, w//2 - len(prompt)//2), prompt)
            btn_text = "[Quit]  [Abort]  [START SHRED]"
            stdscr.addstr(btn_row,0, btn_text[:w-1], curses.A_BOLD | curses.A_REVERSE)

        elif mode == MODE_SHREDDING:
            split_x = w // 2
            left_title = "📌 Processing Path"
            stdscr.addstr(0, 0, left_title, curses.A_BOLD)
            stdscr.addstr(1, 0, "-" * (split_x - 1))
            left_h = btn_row - 3
            if current_global_idx < len(total_file_list):
                curr_path = total_file_list[current_global_idx]
                stdscr.addstr(2, 0, f">>> {curr_path}"[:split_x-1], curses.A_REVERSE)
                hist_y = 3
                for hist_idx in range(max(0, current_global_idx - (left_h - 3)), current_global_idx):
                    fp = total_file_list[hist_idx]
                    stdscr.addstr(hist_y, 0, f"✓ {fp}"[:split_x-1])
                    hist_y += 1
                    if hist_y >= left_h + 2:
                        break
            right_title = "✅ Shred Status"
            stdscr.addstr(0, split_x, right_title, curses.A_BOLD)
            stdscr.addstr(1, split_x, "-" * (w - split_x -1))
            global_done = current_global_idx
            global_total = total_file_count
            global_pct = int(global_done / global_total * 100) if global_total>0 else 0
            stdscr.addstr(2, split_x, f"Total: {global_done}/{global_total}  {global_pct}%")
            bar_w = w - split_x - 4
            bar_fill = int(bar_w * global_pct / 100)
            stdscr.addstr(3, split_x, f"[{'#'*bar_fill}{'-'*(bar_w-bar_fill)}]")
            stdscr.addstr(5, split_x, f"Current File: {current_file_pct}%")
            stdscr.addstr(6, split_x, f"Status: {current_file_status}")
            stdscr.addstr(btn_row,0,"[Quit] (Only available after finish)", curses.A_REVERSE)

        elif mode == MODE_CMDLINE:
            stdscr.addstr(0,0,f"📂 Command mode | ESC cancel | cmd: {cmd_buffer}", curses.A_BOLD)
            if cmd_error:
                stdscr.addstr(1,0,f"⚠️ {cmd_error}", curses.A_REVERSE)
            stdscr.addstr(btn_row,0,f":{cmd_buffer}"[:w-1], curses.A_REVERSE)

        stdscr.refresh()

        curses.halfdelay(5)
        key = stdscr.getch()
        curses.cbreak()

        # ========== 鼠标/触控事件 ==========
        if key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                clicked = bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED)
                if clicked:
                    if my == btn_row and mode == MODE_LIST:
                        _gap = 2
                        _x = 0
                        for (btn_name, _x1, _x2, action) in btn_defs:
                            _x2 = _x + len(btn_name) - 1
                            if _x <= mx <= _x2:
                                if action == "quit":
                                    return
                                elif action == "sel_all":
                                    selected_set.clear()
                                    for idx, entry in enumerate(original_scan_entries):
                                        if not entry.get("is_parent_link", False):
                                            selected_set.add(idx)
                                elif action == "clear_sel":
                                    selected_set.clear()
                                elif action == "confirm":
                                    if len(selected_set) != 0:
                                        all_selected_entries = [original_scan_entries[i] for i in sorted(selected_set)
                                                                if not original_scan_entries[i].get("is_parent_link", False)]
                                        mode = MODE_PREVIEW
                                break
                            _x = _x2 + _gap + 1
                    elif mode == MODE_LIST and not search_mode:
                        disp_line = my - 2
                        max_lines = btn_row - 4
                        if 0 <= disp_line < max_lines:
                            clicked_idx = view_offset + disp_line
                            if clicked_idx < len(scan_entries):
                                cursor_idx = clicked_idx
                                ent_clicked = scan_entries[cursor_idx]
                                ridx = original_scan_entries.index(ent_clicked)
                                if ridx in selected_set:
                                    selected_set.discard(ridx)
                                else:
                                    selected_set.add(ridx)
                if bstate & curses.BUTTON4_PRESSED:
                    cursor_idx = max(0, cursor_idx - 1)
                elif bstate & curses.BUTTON5_PRESSED:
                    cursor_idx = min(len(scan_entries) - 1, cursor_idx + 1)
            except Exception:
                pass
            continue

        # ----解析 Alt‑W / Alt‑Q (ESC + char)----
        if key == 27:
            curses.halfdelay(3)
            k2 = stdscr.getch()
            curses.cbreak()
            if k2 == ord('w'):
                goto_next_match()
                continue
            elif k2 == ord('q'):
                goto_prev_match()
                continue

        # ========== 键盘主逻辑 ==========
        if mode == MODE_LIST:
            if search_mode:
                search_matches = compute_search_matches(search_keyword)
                if key == 27:
                    search_mode = False
                elif key == ord('\n'):
                    search_mode = False
                    if search_matches:
                        current_match_idx = 0
                        goto_next_match()
                elif key == curses.KEY_BACKSPACE or key == 127:
                    search_keyword = search_keyword[:-1]
                elif 32 <= key <= 126:
                    search_keyword += chr(key)
                continue

            if key == ord('/'):
                search_mode = True
            elif key == curses.KEY_UP:
                cursor_idx = max(0, cursor_idx - 1)
            elif key == curses.KEY_DOWN:
                cursor_idx = min(len(scan_entries)-1, cursor_idx +1)
            elif key == ord(' '):
                if len(scan_entries) > 0:
                    real_original_idx = original_scan_entries.index(scan_entries[cursor_idx])
                    if real_original_idx in selected_set:
                        selected_set.discard(real_original_idx)
                    else:
                        selected_set.add(real_original_idx)
            elif key == ord('\n'):
                if len(scan_entries) == 0:
                    continue
                curr_ent = scan_entries[cursor_idx]
                if curr_ent.get("is_parent_link", False):
                    old_pwd = current_pwd
                    current_pwd = curr_ent["path"]
                    rescan_directory()
                elif curr_ent["is_dir"]:
                    old_pwd = current_pwd
                    current_pwd = curr_ent["path"]
                    rescan_directory()
                else:
                    if len(selected_set) == 0:
                        real_idx = original_scan_entries.index(curr_ent)
                        selected_set.add(real_idx)
                    all_selected_entries = [original_scan_entries[i] for i in sorted(selected_set)
                                            if not original_scan_entries[i].get("is_parent_link", False)]
                    mode = MODE_PREVIEW
            elif key == ord('q'):
                return
            elif key == ord(':'):
                mode = MODE_CMDLINE
                cmd_buffer = ""
                cmd_error = ""
            elif key == ord('n'):
                goto_next_match()
            elif key == ord('N'):
                goto_prev_match()

        elif mode == MODE_CMDLINE:
            if key == 27:
                mode = MODE_LIST
                cmd_buffer = ""
                cmd_error = ""
            elif key == ord('\n'):
                parts = cmd_buffer.strip().split(maxsplit=1)
                cmd_error = ""
                if not parts:
                    mode = MODE_LIST
                    continue
                if parts[0] == "cd":
                    arg = parts[1] if len(parts)>=2 else "."
                    try:
                        if arg == "-":
                            target = old_pwd
                        else:
                            target = os.path.abspath(os.path.join(current_pwd, arg))
                        if os.path.isdir(target):
                            old_pwd = current_pwd
                            current_pwd = target
                            rescan_directory()
                        else:
                            cmd_error = f"no such directory: {target}"
                    except Exception as e:
                        cmd_error = str(e)
                else:
                    cmd_error = f"unknown cmd: {parts[0]}, only cd supported"
                if not cmd_error:
                    mode = MODE_LIST
            elif key == curses.KEY_BACKSPACE or key ==127:
                cmd_buffer = cmd_buffer[:-1]
            elif 32 <= key <=126:
                cmd_buffer += chr(key)

        elif mode == MODE_PREVIEW:
            if key == ord('\n'):
                mode = MODE_CONFIRM1
            elif key == 27:
                mode = MODE_LIST
        elif mode == MODE_CONFIRM1:
            if key == ord('\n'):
                mode = MODE_CONFIRM2
            elif key == 27:
                mode = MODE_LIST
        elif mode == MODE_CONFIRM2:
            if key == ord('\n'):
                total_file_list.clear()
                for ent in all_selected_entries:
                    flist = collect_all_files(ent["path"])
                    total_file_list.extend(flist)
                total_file_count = len(total_file_list)
                current_global_idx = 0
                mode = MODE_SHREDDING
            elif key == 27:
                mode = MODE_LIST
        elif mode == MODE_SHREDDING:
            if current_global_idx >= total_file_count:
                stdscr.clear()
                stdscr.addstr(0,0,"🎉 All shred tasks finished!", curses.A_BOLD)
                stdscr.addstr(2,0,f"Total shredded files: {total_file_count}")
                stdscr.addstr(4,0,"Press any key back to directory browser")
                stdscr.refresh()
                stdscr.getch()
                rescan_directory()
                search_mode = False
                search_keyword = ""
                search_matches = []
                mode = MODE_LIST
                continue
            if current_file_generator is None:
                fp = total_file_list[current_global_idx]
                current_file_generator = shred_single_file(fp, passes=SHRED_PASSES)
            try:
                current_file_pct, current_file_status = next(current_file_generator)
            except StopIteration:
                current_global_idx += 1
                current_file_generator = None
                current_file_pct = 0
                current_file_status = "Waiting next file"

if __name__ == "__main__":
    USE_SYSTEM_SHRED = True
    if not os.path.exists("/storage/emulated/0"):
        print("[!] Run `termux-setup-storage` first to grant storage permission")
        sys.exit(1)
    print("===== ✅ TOUCH BUTTON MODE =====")
    print("👉 Long press Termux → MORE → ENABLE MOUSE (必须打开！)")
    print("👉 / 唤起搜索；n=下一个匹配，N=上一个匹配；Alt‑W/Q也支持")
    print("👉 :进入命令行，支持 cd xxx / cd -")
    print("👉 Shred mode: {}".format("system shred command(-v progress)" if USE_SYSTEM_SHRED else "python internal overwrite"))
    print("👉 Need system shred: pkg install coreutils")
    print("👉 Bottom line: [Quit] [SelectAll] [ClearSel] [Confirm] 直接屏幕点击即可")
    print("====================================")
    curses.wrapper(tui_main)
