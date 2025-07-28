# Copyright (C) 2018-2022 Kataiser & https://github.com/Kataiser/tf2-rich-presence/contributors
# https://github.com/Kataiser/tf2-rich-presence/blob/master/LICENSE
# cython: language_level=3

import dataclasses
import functools
import os
import re
from tkinter import messagebox
from typing import Dict, List, Optional, Set, Tuple, Pattern

import settings


@dataclasses.dataclass
class ConsoleLogParsed:
    in_menus: bool = True
    tf2_map: str = ''
    tf2_class: str = ''
    queued_state: str = "Not queued"
    hosting: bool = False
    server_name: str = ''
    server_players: int = 0
    server_players_max: int = 0


# reads a console.log and returns as much game state as possible, alternatively None if whether an old scan was reused
def interpret(self, console_log_path: str, user_usernames: Set[str], kb_limit: float = float(settings.get('console_scan_kb')),
              force: bool = False, tf2_start_time: int = 0) -> Optional[ConsoleLogParsed]:
    TF2_LOAD_TIME_ASSUMPTION: int = 10
    SIZE_LIMIT_MULTIPLE_TRIGGER: int = 4
    SIZE_LIMIT_MULTIPLE_TARGET: int = 2
    SIZE_LIMIT_MIN_LINES: int = 15000
    KATAISER_LOOP_FREQ: int = 4

    # defaults
    default_state = ConsoleLogParsed()
    in_menus: bool = default_state.in_menus
    tf2_map: str = default_state.tf2_map
    tf2_class: str = default_state.tf2_class
    queued_state: str = default_state.queued_state
    hosting: bool = default_state.hosting
    server_name: str = default_state.server_name
    server_players: int = default_state.server_players
    server_players_max: int = default_state.server_players_max

    # console.log is a log of tf2's console (duh), only exists if tf2 has -condebug (see no_condebug_warning() in GUI)
    self.log.debug(f"Looking for console.log at {console_log_path}")

    if not os.path.isfile(console_log_path):
        self.log.error(f"console.log doesn't exist, issuing warning (files/dirs in /tf/: {os.listdir(os.path.dirname(console_log_path))})", reportable=False)
        self.no_condebug = False
        return default_state  # might as well

    # only interpret console.log again if it's been modified
    self.console_log_mtime = int(os.stat(console_log_path).st_mtime)
    if not force and self.console_log_mtime == self.old_console_log_mtime and not self.gui.clean_console_log:
        self.log.debug("Not rescanning console.log")
        return None

    # TF2 takes some time to load the console when starting up, so wait until it's been modified to avoid getting outdated information
    console_log_mtime_relative: int = self.console_log_mtime - tf2_start_time
    if console_log_mtime_relative <= TF2_LOAD_TIME_ASSUMPTION:
        self.log.debug(f"console.log's mtime relative to TF2's start time is {console_log_mtime_relative} (<= {TF2_LOAD_TIME_ASSUMPTION}), assuming default state")
        return default_state

    consolelog_file_size: int = os.stat(console_log_path).st_size
    byte_limit: float = kb_limit * 1024.0

    if self.last_console_log_size is not None:
        if consolelog_file_size < self.last_console_log_size:
            self.log.error("console.log seems to have been externally shortened (possibly TF2BD)")
            # TODO: try to account for this somehow, if need be

        self.last_console_log_size = consolelog_file_size

    # actually open the file finally
    with open(console_log_path, 'r', errors='replace', encoding='UTF8') as consolelog_file:
        if consolelog_file_size > byte_limit:
            skip_to_byte: int = consolelog_file_size - int(byte_limit)
            consolelog_file.seek(skip_to_byte, 0)  # skip to last few KBs

            lines: List[str] = consolelog_file.readlines()
            self.log.debug(f"console.log: {consolelog_file_size} bytes, skipped to {skip_to_byte}, read {int(byte_limit)} bytes and {len(lines)} lines")
        else:
            lines = consolelog_file.readlines()
            self.log.debug(f"console.log: {consolelog_file_size} bytes, {len(lines)} lines (didn't skip lines)")

    # update this again late, fixes wrong detections but may cause a duplicate scan
    self.console_log_mtime = int(os.stat(console_log_path).st_mtime)

    # limit the file size, for better readlines performance
    if consolelog_file_size > byte_limit * SIZE_LIMIT_MULTIPLE_TRIGGER and len(lines) > SIZE_LIMIT_MIN_LINES and settings.get('trim_console_log') and not force:
        trim_size = int(byte_limit * SIZE_LIMIT_MULTIPLE_TARGET)
        self.log.debug(f"Limiting console.log to {trim_size} bytes")

        try:
            with open(console_log_path, 'rb+') as consolelog_file_b:
                # this can probably be done faster and/or cleaner
                consolelog_file_b.seek(-trim_size, 2)
                consolelog_file_trimmed: bytes = consolelog_file_b.read()
                trimmed_line_count: int = consolelog_file_trimmed.count(b'\n')

                if trimmed_line_count > SIZE_LIMIT_MIN_LINES:
                    consolelog_file_b.seek(0)
                    consolelog_file_b.truncate()
                    consolelog_file_b.write(consolelog_file_trimmed)
                else:
                    self.log.error(f"Trimmed line count will be {trimmed_line_count} (< {SIZE_LIMIT_MIN_LINES}), aborting (trim len = {len(consolelog_file_trimmed)})")
        except PermissionError as error:
            self.log.error(f"Failed to trim console.log: {error}")

    # setup
    now_in_menus: bool = False
    just_started_server: bool = False
    server_still_running: bool = False
    using_wav_cache: bool = False
    connecting_to_matchmaking: bool = False
    found_first_wav_cache: bool = False
    with_optimization: bool = True  # "with" optimization, not "with optimization"
    chat_safety: bool = True
    self.kataiser_scan_loop += 1
    kataiser_scan: bool = self.kataiser_scan_loop == KATAISER_LOOP_FREQ if not force else True
    if kataiser_scan:
        self.kataiser_scan_loop = 0
    user_is_kataiser: bool = 'Kataiser' in user_usernames
    kataiser_seen_on: str = ''
    # TODO: detection for canceling loading into community servers (if possible)
    match_types: Dict[str, str] = {'12v12 Casual Match': 'Casual', 'MvM Practice': 'MvM (Boot Camp)', 'MvM MannUp': 'MvM (Mann Up)', '6v6 Ladder Match': 'Competitive'}
    menus_messages: Tuple[str, ...] = ('For FCVAR_REPLICATED', '[TF Workshop]', 'request to abandon', 'Server shutting down', 'Lobby destroyed', 'Disconnect:', 'destroyed CAsyncWavDataCache',
                                       'ShutdownGC', 'Connection failed after', 'Host_Error')
    menus_message_used: Optional[str] = None
    menus_message: str
    gui_update: int = 0
    gui_updates: int = 0

    for username in user_usernames:
        if 'with' in username:
            with_optimization = False
        if ' :  ' in username:
            chat_safety = False

    # iterates though 0 (initially) to roughly 16000 lines from console.log and learns (almost) everything from them
    line: str
    for line in lines:
        gui_update += 1

        if gui_update == 1500:
            # update the GUI occasionally, to prevent UI lag
            self.gui.safe_update()
            gui_update = 0
            gui_updates += 1

        # lines that have "with" in them are basically always kill logs and can be safely ignored
        # this (probably) improves performance
        # same goes for chat logs, this one's actually to reduce false detections
        if (with_optimization and 'with' in line) or (chat_safety and ' :  ' in line):
            if not kataiser_scan or user_is_kataiser or 'Kataiser' not in line:
                continue

        if not in_menus:
            for menus_message in menus_messages:
                if menus_message in line:
                    now_in_menus = True
                    break

            if line.startswith('hostname: '):
                server_name = line[10:-1]

            elif line.startswith('players : '):
                line_split = line.split()
                server_players = int(line_split[2]) + int(line_split[4])  # humans + bots
                server_players_max = int(line_split[6][1:])

            elif line.endswith(' selected \n'):
                class_line_possibly: List[str] = line[:-11].split()

                if class_line_possibly and class_line_possibly[-1] in tf2_classes:
                    tf2_class = class_line_possibly[-1]

            elif 'Disconnect by user' in line:
                for user_username in user_usernames:
                    if user_username in line:
                        now_in_menus = True
                        break

            elif 'Missing map' in line and 'Missing map material' not in line:
                now_in_menus = True

            if kataiser_scan and not user_is_kataiser and '[U:1:160315024]' in line:
                kataiser_seen_on = tf2_map

        elif 'SV_ActivateServer' in line:  # full line: "SV_ActivateServer: setting tickrate to 66.7"
            just_started_server = True

        if line.startswith('Map:'):
            in_menus = False
            tf2_map = line[5:-1]
            tf2_class = ''

            if just_started_server:
                server_still_running = True
                just_started_server = False
            else:
                just_started_server = False
                server_still_running = False

        elif not connecting_to_matchmaking and 'Connected to' in line:
            # joined a community server, so must use CAsyncWavDataCache method to detect disconnects
            using_wav_cache = True
            found_first_wav_cache = False
            connecting_to_matchmaking = False

        elif 'matchmaking server' in line:
            connecting_to_matchmaking = True

        elif using_wav_cache and 'CAsyncWavDataCache' in line:
            if found_first_wav_cache:
                # it's the one after disconnecting

                if in_menus:
                    # ...unless it isn't?
                    self.log.error("Found CAsyncWavDataCache despite being in menus already")
                else:
                    now_in_menus = True
            else:
                # it's the one after loading in
                found_first_wav_cache = True

        elif '[P' in line:
            if '[PartyClient] L' in line:  # full line: "[PartyClient] Leaving queue"
                # queueing is not necessarily only in menus
                queued_state = "Not queued"

            elif '[PartyClient] Entering q' in line:  # full line: "[PartyClient] Entering queue for match group " + whatever mode
                match_type: str = line.split('match group ')[-1][:-1]
                queued_state = f"Queued for {match_types[match_type]}"

            elif '[PartyClient] Entering s' in line:  # full line: "[PartyClient] Entering standby queue"
                queued_state = 'Queued for a party\'s match'

        if now_in_menus:
            now_in_menus = False
            in_menus = True
            menus_message_used = line
            kataiser_seen_on = ''
            connecting_to_matchmaking = False
            using_wav_cache = False
            found_first_wav_cache = False

    if not user_is_kataiser and not in_menus and kataiser_seen_on == tf2_map:
        self.log.debug(f"Kataiser located, telling user :D (on {tf2_map})")
        self.gui.set_bottom_text('kataiser', True)

    if in_menus:
        tf2_map = ''
        tf2_class = ''
        hosting = False
        server_name = ''
        server_players = 0
        server_players_max = 0
        self.gui.set_bottom_text('kataiser', False)

        if menus_message_used:
            self.log.debug(f"Menus message used: \"{menus_message_used.strip()}\"")
    else:
        server_name, is_valve_server = cleanup_server_name(server_name)

        if is_valve_server and server_players_max == 32:
            server_players_max = 24  # cool

        if tf2_class != '' and tf2_map == '':
            self.log.error("Have class without map")

        if server_still_running:
            hosting = True

    if settings.get('hide_queued_gamemode') and "Queued" in queued_state:
        self.log.debug(f"Hiding queued state (\"{queued_state}\" to \"Queued\")")
        queued_state = "Queued"

    scan_results = ConsoleLogParsed(in_menus, tf2_map, tf2_class, queued_state, hosting, server_name, server_players, server_players_max)
    self.log.debug(f"console.log parse results: {scan_results}")

    if gui_updates != 0:
        self.log.debug(f"Mid-parse GUI updates: {gui_updates}")

    # remove empty lines (bot spam probably) and some error logs
    # TODO: move this into a function
    if (in_menus and settings.get('trim_console_log') and not force and self.cleanup_primed) or self.gui.clean_console_log:
        if self.gui.clean_console_log:
            self.log.debug("Forcing cleanup of console.log")
        else:
            self.log.debug("Potentially cleaning up console.log")

        console_log_lines_out: List[str] = []
        total_line_count: int = 0
        blank_line_count: int = 0
        error_line_count: int = 0

        with open(console_log_path, 'r', encoding='UTF8', errors='replace') as console_log_read:
            console_log_lines_in: List[str] = console_log_read.readlines()

        error_substrings: Tuple[str, ...] = ('bad reference count', 'particle system', 'DataTable warning', 'SOLID_VPHYSICS', 'BlockingGetDataPointer', 'No such variable')
        if user_is_kataiser:
            error_substrings += ('Usage: spec_player',)  # cause I have a bind that errors a lot

        for line in console_log_lines_in:
            remove_line: bool = False

            for error_substring in error_substrings:
                if error_substring in line and ' :  ' not in line:
                    remove_line = True
                    error_line_count += 1
                    break

            if not remove_line:
                if line.strip(' \t') == '\n':
                    remove_line = True
                    blank_line_count += 1

            if remove_line:
                total_line_count += 1
            else:
                console_log_lines_out.append(line)

        line_count_text: str = f"{error_line_count} error lines and {blank_line_count} blank lines (total: {total_line_count})"

        if total_line_count >= (1 if self.gui.clean_console_log else 50):
            with open(console_log_path, 'w', encoding='UTF8') as console_log_write:
                for line in console_log_lines_out:
                    console_log_write.write(line)

            self.last_console_log_size = os.stat(console_log_path).st_size
            self.log.debug(f"Removed {line_count_text} from console.log")
        else:
            self.log.debug(f"Didn't remove {line_count_text} from console.log")

        if self.gui.clean_console_log:
            self.gui.pause()
            messagebox.showinfo("TF2 Rich Presence", f"Removed {line_count_text} from console.log.")
            self.gui.unpause()
            self.gui.clean_console_log = False

        self.cleanup_primed = False
    else:
        self.cleanup_primed = True

    return scan_results


# check if any characters outside of ASCII exist in any usernames
def non_ascii_in_usernames(usernames: Set[str]) -> bool:
    for username in usernames:
        if non_ascii_regex.search(username) is not None:
            return True

    return False


# make server names look a bit nicer
@functools.cache
def cleanup_server_name(name: str) -> tuple[str, bool]:
    if re_valve_server.match(name):
        return re_valve_server_remove.sub("", name), True
    else:
        name = ''.join(c for c in name if c.isprintable() and c not in ('█', '▟', '▙')).strip()  # removes unprintable/ugly characters
        name = re_double_space.sub(' ', name)  # removes double space

        if len(name) > 32:
            # TODO: would prefer to use actual text width here
            return f'{name[:30]}…', False
        else:
            return name, False


re_valve_server: Pattern[str] = re.compile(r'Valve Matchmaking Server \([a-zA-Z]+ srcds[0-9]+-[a-zA-Z]+\d #[0-9]+\)')
re_valve_server_remove: Pattern[str] = re.compile(r' srcds[0-9]+-[a-zA-Z]+\d #[0-9]+')
re_double_space: Pattern[str] = re.compile(r' {2,}')
tf2_classes: Tuple[str, ...] = ('Scout', 'Soldier', 'Pyro', 'Demoman', 'Heavy', 'Engineer', 'Medic', 'Sniper', 'Spy')
non_ascii_regex = re.compile('[^\x00-\x7F]')
