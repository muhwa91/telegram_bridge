"""bridge.py 순수 함수 단위 테스트 (계약 기반).

계약 출처: docs/features/telegram_bridge/01-계획.md "순수 함수 계약" 섹션.
    parse_message(text) -> tuple[str, str] | None
    is_allowed(chat_id, allowed: frozenset[int]) -> bool
    resolve_project(name, target_root) -> str | None
    chunk_text(text, limit=4096) -> list[str]
    mask_secrets(text, secrets: list[str]) -> str

표준 pytest 만 사용(내장 tmp_path 픽스처 허용). 네트워크·subprocess 호출 없음.
bridge.py 가 병렬 구현 중이라 임포트가 실패할 수 있으나, 파일 작성이 산출물이다.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import bridge
import pytest
from bridge import (
    chunk_text,
    event_to_progress,
    format_reply,
    handle_callback,
    is_allowed,
    mask_secrets,
    parse_callback,
    parse_message,
    project_keyboard,
    push_keyboard,
    resolve_project,
    run_claude,
)


def _assistant(*blocks):
    """assistant 이벤트 헬퍼 — message.content 블록 리스트로 감싼다."""
    return {"type": "assistant", "message": {"content": list(blocks)}}


# ---------------------------------------------------------------------------
# parse_message: "<프로젝트> <지시...>" → (project, task) / 커맨드·형식불일치는 None
# ---------------------------------------------------------------------------


def test_parse_message_normal_two_words():
    assert parse_message("trading_info 헤더고쳐줘") == ("trading_info", "헤더고쳐줘")


def test_parse_message_multiword_task():
    # 첫 토큰만 프로젝트, 나머지 전체가 지시(공백 포함 보존)
    assert parse_message("trading_info 헤더를 3행으로 정렬해줘") == (
        "trading_info",
        "헤더를 3행으로 정렬해줘",
    )


def test_parse_message_strips_surrounding_whitespace():
    assert parse_message("   trading_info   헤더 고쳐줘  ") == (
        "trading_info",
        "헤더 고쳐줘",
    )


def test_parse_message_single_word_is_none():
    # 지시 없이 프로젝트명만 → None
    assert parse_message("trading_info") is None


def test_parse_message_empty_string_is_none():
    assert parse_message("") is None


def test_parse_message_whitespace_only_is_none():
    assert parse_message("     ") is None


def test_parse_message_push_command_is_none():
    assert parse_message("push") is None


def test_parse_message_help_command_is_none():
    assert parse_message("/help") is None


def test_parse_message_projects_command_is_none():
    assert parse_message("/projects") is None


# ---------------------------------------------------------------------------
# is_allowed(chat_id, allowed)
# ---------------------------------------------------------------------------


def test_is_allowed_true_when_in_set():
    assert is_allowed(12345, frozenset({12345, 67890})) is True


def test_is_allowed_false_when_not_in_set():
    assert is_allowed(99999, frozenset({12345, 67890})) is False


def test_is_allowed_false_when_empty_allowlist():
    # 허용목록이 비면 아무도 통과 못 함(허용목록 제거=전면 차단, 보안 기본값)
    assert is_allowed(12345, frozenset()) is False


# ---------------------------------------------------------------------------
# resolve_project: target_root 직속 폴더명 정확 일치만 / 트래버설 거부
# ---------------------------------------------------------------------------


def test_resolve_project_exact_match_success(tmp_path):
    (tmp_path / "trading_info").mkdir()
    result = resolve_project("trading_info", str(tmp_path))
    assert result is not None
    # 반환이 절대/상대 어느 쪽이든 실제 대상 폴더를 가리켜야 한다
    assert Path(result).name == "trading_info"
    assert Path(result).is_dir()


def test_resolve_project_case_mismatch_rejected(tmp_path):
    # Windows 파일시스템은 대소문자 무시라, 정확 일치는 listdir 문자열 비교여야 함.
    # 나이브한 os.path.isdir(join(root, name)) 구현이면 이 테스트가 잡아낸다.
    (tmp_path / "trading_info").mkdir()
    assert resolve_project("Trading_Info", str(tmp_path)) is None


def test_resolve_project_partial_match_rejected(tmp_path):
    (tmp_path / "trading_info").mkdir()
    assert resolve_project("trading", str(tmp_path)) is None


def test_resolve_project_nonexistent_rejected(tmp_path):
    (tmp_path / "trading_info").mkdir()
    assert resolve_project("etf_info", str(tmp_path)) is None


def test_resolve_project_parent_traversal_rejected(tmp_path):
    assert resolve_project("..", str(tmp_path)) is None


def test_resolve_project_forward_slash_rejected(tmp_path):
    (tmp_path / "a").mkdir()
    assert resolve_project("a/b", str(tmp_path)) is None


def test_resolve_project_backslash_rejected(tmp_path):
    (tmp_path / "a").mkdir()
    assert resolve_project("a\\b", str(tmp_path)) is None


def test_resolve_project_absolute_path_rejected(tmp_path):
    # 실재하는 폴더의 절대경로라도, 폴더명 아닌 경로면 거부
    real = tmp_path / "realproj"
    real.mkdir()
    assert resolve_project(str(real), str(tmp_path)) is None


def test_resolve_project_empty_name_rejected(tmp_path):
    assert resolve_project("", str(tmp_path)) is None


# ---------------------------------------------------------------------------
# chunk_text(text, limit=4096)
# ---------------------------------------------------------------------------


def test_chunk_text_under_limit_single_chunk():
    text = "a" * 100
    assert chunk_text(text) == [text]


def test_chunk_text_exactly_at_limit_single_chunk():
    text = "a" * 4096
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_text_one_over_limit_splits_into_two():
    # 경계 검증: 4097자 → 2개 [4096, 1]
    text = "a" * 4097
    chunks = chunk_text(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == 4096
    assert len(chunks[1]) == 1


def test_chunk_text_empty_returns_list_with_empty_string():
    # 계약: 빈 문자열이면 [""] (빈 리스트가 아님)
    assert chunk_text("") == [""]


def test_chunk_text_every_chunk_within_limit():
    text = "b" * (4096 * 2 + 37)
    chunks = chunk_text(text)
    assert len(chunks) == 3
    assert all(len(c) <= 4096 for c in chunks)


def test_chunk_text_reconstructs_original_no_data_loss():
    # 분할이 데이터를 잃거나 중복시키지 않아야 한다
    text = "가나다" * 5000
    assert "".join(chunk_text(text)) == text


def test_chunk_text_custom_limit():
    chunks = chunk_text("abcde", limit=2)
    assert chunks == ["ab", "cd", "e"]
    assert all(len(c) <= 2 for c in chunks)


# ---------------------------------------------------------------------------
# mask_secrets(text, secrets)
# ---------------------------------------------------------------------------


def test_mask_secrets_single_value():
    assert mask_secrets("token=abc123", ["abc123"]) == "token=***"


def test_mask_secrets_multiple_values():
    result = mask_secrets("id=42 token=xyz", ["42", "xyz"])
    assert result == "id=*** token=***"


def test_mask_secrets_all_occurrences_replaced():
    # 같은 비밀값이 여러 번 나오면 전부 치환
    assert mask_secrets("xyz and xyz", ["xyz"]) == "*** and ***"


def test_mask_secrets_empty_list_keeps_original():
    assert mask_secrets("nothing secret here", []) == "nothing secret here"


def test_mask_secrets_empty_secret_string_does_not_destroy_text():
    # 빈 비밀문자열("")은 무시돼야 한다. 나이브한 str.replace("", "***")는
    # 모든 글자 사이에 ***를 삽입해 텍스트를 파괴/폭증시킨다 → 그 버그를 잡는다.
    result = mask_secrets("hello", ["", "ell"])
    assert result == "h***o"


def test_mask_secrets_only_empty_secret_keeps_original():
    result = mask_secrets("hello", [""])
    assert result == "hello"


# ---------------------------------------------------------------------------
# format_reply(data): claude JSON 결과 → 텔레그램 회신 텍스트 (순수 함수)
# ---------------------------------------------------------------------------


def test_format_reply_success_header_no_cost():
    reply = format_reply({"result": "작업 완료", "is_error": False, "total_cost_usd": 0.05})
    assert reply.startswith("[ ✅처리완료 ]")
    assert "작업 완료" in reply
    # 비용은 표시하지 않는다(정책). 고정 push/커밋 안내도 붙이지 않는다
    # (실제 커밋/푸시 안내는 handle_update 가 git 상태를 조회해 덧붙임).
    assert "비용" not in reply
    assert "push" not in reply
    assert "커밋" not in reply


def test_format_reply_error_header():
    reply = format_reply({"result": "실행 실패", "is_error": True})
    assert reply.startswith("[ ❌처리실패 ]")
    assert "실행 실패" in reply
    assert "비용" not in reply


def test_format_reply_empty_result_header_only():
    reply = format_reply({"result": "", "is_error": False})
    assert reply == "[ ✅처리완료 ]"
    assert "비용" not in reply


def test_format_reply_error_empty_result_header_only():
    # 실패 + 빈 result → 실패 헤더 단독
    reply = format_reply({"result": "", "is_error": True})
    assert reply == "[ ❌처리실패 ]"


# ---------------------------------------------------------------------------
# event_to_progress(event): stream-json 이벤트 → 진행 표시 한 줄 (순수 함수, 계약)
# 스키마는 실제 `claude --output-format stream-json --verbose` 방출을 실측해 맞춤.
# ---------------------------------------------------------------------------


def test_event_to_progress_text_narration():
    ev = _assistant({"type": "text", "text": "파일 목록을 확인합니다"})
    assert event_to_progress(ev) == "파일 목록을 확인합니다"


def test_event_to_progress_text_truncated_to_120():
    ev = _assistant({"type": "text", "text": "가" * 200})
    result = event_to_progress(ev)
    assert result == "가" * 120


def test_event_to_progress_text_stripped():
    ev = _assistant({"type": "text", "text": "  여백 제거  "})
    assert event_to_progress(ev) == "여백 제거"


def test_event_to_progress_masks_secret_before_truncation():
    # L-1: 경계에서 비밀값이 쪼개져 조각이 새지 않도록, 잘라내기 전에 마스킹해야 한다.
    secret = "C:\\Users\\Home"
    # secret 이 60자 경계에 걸치도록 배치(prefix 55 → secret 이 55~68 위치).
    # 마스킹을 안 하면 [:60] 이 "...C:\\Us" 같은 조각을 남긴다.
    cmd = "a" * 55 + secret + "tail"
    ev = _assistant({"type": "tool_use", "name": "Bash", "input": {"command": cmd}})
    line = event_to_progress(ev, [secret])
    assert secret not in line  # 잘린 조각도 없어야 함
    assert "C:\\Us" not in line  # 경계에서 쪼개진 조각도 없어야 함
    assert "***" in line


def test_event_to_progress_empty_text_is_none():
    ev = _assistant({"type": "text", "text": "   "})
    assert event_to_progress(ev) is None


def test_event_to_progress_read_basename_only():
    ev = _assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "E:/a/b/br.py"}})
    assert event_to_progress(ev) == "📖 읽음: br.py"


def test_event_to_progress_edit_basename():
    ev = _assistant({"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/y/app.py"}})
    assert event_to_progress(ev) == "✏️ 수정: app.py"


def test_event_to_progress_write_basename():
    ev = _assistant({"type": "tool_use", "name": "Write", "input": {"file_path": "note.md"}})
    assert event_to_progress(ev) == "✏️ 수정: note.md"


def test_event_to_progress_bash_command_prefix():
    ev = _assistant({"type": "tool_use", "name": "Bash", "input": {"command": "git commit -m x"}})
    assert event_to_progress(ev) == "⚡ 실행: git commit -m x"


def test_event_to_progress_bash_command_truncated_to_60():
    ev = _assistant({"type": "tool_use", "name": "Bash", "input": {"command": "a" * 100}})
    assert event_to_progress(ev) == "⚡ 실행: " + "a" * 60


def test_event_to_progress_other_tool_generic_icon():
    ev = _assistant({"type": "tool_use", "name": "Glob", "input": {"pattern": "*"}})
    assert event_to_progress(ev) == "🔧 Glob"


def test_event_to_progress_thinking_is_none():
    ev = _assistant({"type": "thinking", "thinking": "내부 추론", "signature": "x"})
    assert event_to_progress(ev) is None


def test_event_to_progress_system_init_is_none():
    assert event_to_progress({"type": "system", "subtype": "init", "model": "opus"}) is None


def test_event_to_progress_result_is_none():
    assert event_to_progress({"type": "result", "subtype": "success", "result": "DONE"}) is None


def test_event_to_progress_tool_result_is_none():
    ev = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}]},
    }
    assert event_to_progress(ev) is None


def test_event_to_progress_rate_limit_is_none():
    assert event_to_progress({"type": "rate_limit_event", "rate_limit_info": {}}) is None


def test_event_to_progress_missing_file_path_placeholder():
    ev = _assistant({"type": "tool_use", "name": "Read", "input": {}})
    assert event_to_progress(ev) == "📖 읽음: ?"


def test_event_to_progress_malformed_content_is_none():
    # message.content 가 리스트가 아니면 안전하게 None
    assert event_to_progress({"type": "assistant", "message": {"content": "oops"}}) is None
    assert event_to_progress({"type": "assistant"}) is None


# ---------------------------------------------------------------------------
# git_status_note / do_push: _git 을 monkeypatch(pytest 내장)해 분기 검증
# ---------------------------------------------------------------------------


def _fake_git(mapping):
    """subcommand 튜플 접두어 → (returncode, stdout, stderr) 매핑으로 _git 을 대체."""

    def fake(_root, *args):
        for key, (rc, out, err) in mapping.items():
            if args[: len(key)] == key:
                return subprocess.CompletedProcess(["git", *args], rc, out, err)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    return fake


def test_git_status_note_ahead_dirty(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "3\n", ""), ("status",): (0, " M bridge.py\n", "")}),
    )
    note = bridge.git_status_note(Path())
    assert "3" in note
    assert "미커밋" in note


def test_git_status_note_ahead_clean(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "2\n", ""), ("status",): (0, "", "")}),
    )
    note = bridge.git_status_note(Path())
    assert "2" in note
    assert "미커밋" not in note


def test_git_status_note_no_ahead_dirty(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "0\n", ""), ("status",): (0, " M x.py\n", "")}),
    )
    note = bridge.git_status_note(Path())
    assert note == "변경이 있으나 커밋되지 않았습니다(확인 필요)."


def test_git_status_note_no_ahead_clean(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "0\n", ""), ("status",): (0, "", "")}),
    )
    assert bridge.git_status_note(Path()) == "변경 없음."


def test_git_status_note_revlist_fail_fallback(monkeypatch):
    # rev-list 실패 → ahead 0 안전 폴백(크래시 없이 dirty 만 반영)
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (128, "", "fatal"), ("status",): (0, " M x.py\n", "")}),
    )
    assert bridge.git_status_note(Path()) == "변경이 있으나 커밋되지 않았습니다(확인 필요)."


def test_git_status_note_status_fail_fallback(monkeypatch):
    # status 실패 → dirty False 안전 폴백
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "0\n", ""), ("status",): (1, "", "fatal")}),
    )
    assert bridge.git_status_note(Path()) == "변경 없음."


def test_do_push_pull_fail_aborts(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("pull",): (1, "", "CONFLICT tail"), ("rebase",): (0, "", "")}),
    )
    result = bridge.do_push(Path())
    assert result.startswith(bridge.HEADER_FAIL)
    assert "pull --rebase 실패" in result
    assert "CONFLICT tail" in result


def test_do_push_push_fail(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("pull",): (0, "", ""), ("push",): (1, "", "rejected tail")}),
    )
    result = bridge.do_push(Path())
    assert result.startswith(bridge.HEADER_FAIL)
    assert "push 실패" in result
    assert "rejected tail" in result


def test_do_push_success(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("pull",): (0, "", ""), ("push",): (0, "", "")}),
    )
    result = bridge.do_push(Path())
    assert result.startswith(bridge.HEADER_DONE)


# ---------------------------------------------------------------------------
# project_keyboard / push_keyboard / parse_callback: 인라인 키보드 (순수 함수)
# ---------------------------------------------------------------------------


def test_project_keyboard_empty_no_buttons():
    assert project_keyboard([]) == {"inline_keyboard": []}


def test_project_keyboard_callback_data_prefix():
    kb = project_keyboard(["etf_info"])
    btn = kb["inline_keyboard"][0][0]
    assert btn["text"] == "etf_info"
    assert btn["callback_data"] == "p:etf_info"


def test_project_keyboard_two_per_row():
    kb = project_keyboard(["a", "b", "c", "d", "e"])
    rows = kb["inline_keyboard"]
    # 2개씩 → [a,b][c,d][e]
    assert [len(r) for r in rows] == [2, 2, 1]
    assert rows[0][1]["callback_data"] == "p:b"
    assert rows[2][0]["callback_data"] == "p:e"


def test_project_keyboard_callback_data_within_64_bytes():
    # 초과 이름은 callback_data 만 64바이트로 절단, 표시 text 는 전체.
    long_name = "가" * 100  # 3바이트 * 100 = 300바이트
    kb = project_keyboard([long_name])
    btn = kb["inline_keyboard"][0][0]
    assert btn["text"] == long_name
    assert len(btn["callback_data"].encode("utf-8")) <= 64
    # 부분 멀티바이트가 깨지지 않게 잘려야 한다(디코드 가능).
    assert btn["callback_data"].startswith("p:가")


def test_push_keyboard_structure():
    kb = push_keyboard()
    row = kb["inline_keyboard"][0]
    assert [b["callback_data"] for b in row] == ["push", "x"]
    assert row[0]["text"] == "✅ Push"
    assert row[1]["text"] == "❌ 취소"


def test_parse_callback_push():
    assert parse_callback("push") == ("push", "")


def test_parse_callback_cancel():
    assert parse_callback("x") == ("x", "")


def test_parse_callback_project():
    assert parse_callback("p:trading_info") == ("p", "trading_info")


def test_parse_callback_empty_project_name_rejected():
    assert parse_callback("p:") is None


def test_parse_callback_unknown_rejected():
    assert parse_callback("bogus") is None
    assert parse_callback("") is None
    assert parse_callback("push extra") is None


# ---------------------------------------------------------------------------
# event_to_progress — text 경로 마스킹 (L-1 보강, 순수 함수)
# ---------------------------------------------------------------------------


def test_event_to_progress_text_masks_secret():
    # L-1: assistant text 블록에 비밀값이 섞여도 잘라내기 전에 마스킹돼야 한다.
    secret = "1234567890:ABCsecrettoken"
    ev = _assistant({"type": "text", "text": f"토큰은 {secret} 입니다"})
    line = event_to_progress(ev, [secret])
    assert line is not None
    assert secret not in line
    assert "***" in line


# ===========================================================================
# 아래는 순수 함수가 아닌 통합/보안 테스트 — subprocess·monkeypatch 사용.
#   run_claude 스트리밍 리더(D-1/D-2/D-3) 통합 + handle_callback 인가·라우팅.
# 디버거 재현 시나리오를 가짜 claude 실행 파일로 재현한다.
# ===========================================================================

# 가짜 claude 본체: stdin(task) 내용으로 동작을 분기해 NDJSON 을 stdout 으로 방출한다.
#  - STDERR_FLOOD: result 전에 stderr 로 대량 출력(파이프 버퍼 포화 → 드레인 없으면 데드락)
#  - NO_RESULT   : result 없이 stderr 에 진단 남기고 종료(폴백 경로)
#  - HANG        : result 방출 후 stdout 을 닫지 않고 장기 sleep(손자 fd 점유 재현)
FAKE_CLAUDE_PY = """\
import json
import sys
import time

data = sys.stdin.read()


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


if "STDERR_FLOOD" in data:
    for i in range(3000):
        sys.stderr.write("noise %d filler filler filler filler\\n" % i)
    sys.stderr.flush()

if "NO_RESULT" in data:
    sys.stderr.write("fatal: fake claude crashed\\n")
    sys.stderr.flush()
    sys.exit(3)

emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})
emit({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "DONE_FAKE", "total_cost_usd": 0.01,
})

if "HANG" in data:
    time.sleep(30)
"""


def _fake_claude(tmp_path):
    """가짜 claude 실행 파일(shim)을 만들고 경로를 반환.

    run_claude 가 claude_exe 뒤에 고정 플래그를 붙이므로, shim 은 그 인자를 무시하고
    python 으로 fake 스크립트를 실행한다(stdin/stdout/stderr 상속). 실제 claude 도
    Windows 에선 .CMD 배치 shim 이라 Popen 직접 실행이 동작한다(C-1 실측).
    """
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE_PY, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "fake_claude.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}"\r\n', encoding="utf-8")
    else:
        shim = tmp_path / "fake_claude.sh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8")
        shim.chmod(0o755)
    return str(shim)


def test_run_claude_normal_completion_returns_result(tmp_path):
    # break-on-result(D-2)가 정상(비-행) 작업 완료를 깨지 않는지 — 여전히 result 반환.
    exe = _fake_claude(tmp_path)
    data = run_claude(exe, str(tmp_path), "just do it", timeout=30)
    assert data.get("result") == "DONE_FAKE"
    assert data.get("is_error") is False


def test_run_claude_breaks_on_result_before_timeout(tmp_path):
    # D-2: fake 는 result 방출 후 stdout 을 닫지 않고 30초 sleep. run_claude 는
    # 30초 타임아웃을 기다리지 않고 result 를 즉시 반환해야 한다(break-on-result).
    exe = _fake_claude(tmp_path)
    start = time.monotonic()
    data = run_claude(exe, str(tmp_path), "HANG please", timeout=30)
    elapsed = time.monotonic() - start
    assert data.get("result") == "DONE_FAKE"  # D-3: 잔존 자식이 있어도 정상 결과 반환
    assert data.get("is_error") is False
    assert elapsed < 20  # 30초 데드라인을 안 기다림(break 실패 시 ~30초 → 실패로 검출)


def test_run_claude_stderr_flood_no_deadlock(tmp_path):
    # D-1: result 전에 stderr 로 대량 출력(파이프 버퍼 초과). 드레인 스레드가 배수하지
    # 않으면 자식이 stderr write 에서 블록 → result 미도달 → 거짓 타임아웃.
    exe = _fake_claude(tmp_path)
    start = time.monotonic()
    data = run_claude(exe, str(tmp_path), "STDERR_FLOOD then work", timeout=30)
    elapsed = time.monotonic() - start
    assert data.get("result") == "DONE_FAKE"
    assert elapsed < 20


def test_run_claude_no_result_falls_back_to_stderr(tmp_path):
    # D-1 폴백: result 없이 종료 시 드레인 버퍼(stderr tail)로 진단을 반환.
    exe = _fake_claude(tmp_path)
    data = run_claude(exe, str(tmp_path), "NO_RESULT crash", timeout=30)
    assert data.get("is_error") is True
    assert "fatal" in str(data.get("result", ""))


# ---------------------------------------------------------------------------
# handle_callback — 인라인 버튼 인가·라우팅 (네트워크·push 함수 monkeypatch)
# ---------------------------------------------------------------------------

_ALLOWED = frozenset({777})


@pytest.fixture
def cb_spy(monkeypatch):
    """answer_callback·send_message·edit_message·do_push 를 스파이로 대체."""
    calls = {"answer": [], "send": [], "edit": [], "push": []}

    def fake_answer(_token, cq_id):
        calls["answer"].append(cq_id)

    def fake_send(_token, chat_id, text, _secrets, reply_markup=None):
        calls["send"].append((chat_id, text, reply_markup))

    def fake_edit(_token, chat_id, message_id, text, _secrets):
        calls["edit"].append((chat_id, message_id, text))

    def fake_push(root):
        calls["push"].append(root)
        return bridge.HEADER_DONE + "\n\npush ok"

    monkeypatch.setattr(bridge, "answer_callback", fake_answer)
    monkeypatch.setattr(bridge, "send_message", fake_send)
    monkeypatch.setattr(bridge, "edit_message", fake_edit)
    monkeypatch.setattr(bridge, "do_push", fake_push)
    return calls


def _cq(chat_id, data, cq_id="cq1", message_id=99):
    msg = {"chat": {"id": chat_id}}
    if message_id is not None:
        msg["message_id"] = message_id
    return {"id": cq_id, "from": {"id": chat_id}, "message": msg, "data": data}


def test_callback_disallowed_chat_nothing_called(cb_spy, tmp_path):
    # HIGH: 미허용 chat 은 허용목록 게이트에서 즉시 거부 — answer·push·send 전부 미호출.
    handle_callback(_cq(999, "push"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy == {"answer": [], "send": [], "edit": [], "push": []}


def test_callback_valid_project_sends_guide(cb_spy, tmp_path):
    # HIGH: p:<유효> → resolve_project 통과 시 안내만 발송(push·edit 없음).
    (tmp_path / "etf_info").mkdir()
    handle_callback(_cq(777, "p:etf_info"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy["push"] == []
    assert len(cb_spy["send"]) == 1
    chat_id, text, _markup = cb_spy["send"][0]
    assert chat_id == 777
    assert "etf_info" in text


def test_callback_invalid_project_no_send(cb_spy, tmp_path):
    # HIGH: p:<트래버설> → resolve_project None → 무시(발송 없음).
    handle_callback(_cq(777, "p:../secret"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy["send"] == []
    assert cb_spy["push"] == []


def test_callback_push_calls_do_push_and_edits(cb_spy, tmp_path):
    # MEDIUM: push → do_push 호출 후 결과로 원본 메시지 edit.
    handle_callback(_cq(777, "push"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert len(cb_spy["push"]) == 1
    assert len(cb_spy["edit"]) == 1
    _cid, mid, text = cb_spy["edit"][0]
    assert mid == 99
    assert text.startswith(bridge.HEADER_DONE)


def test_callback_cancel_edits_message(cb_spy, tmp_path):
    # MEDIUM: x → "취소" 로 edit, push 없음.
    handle_callback(_cq(777, "x"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy["push"] == []
    assert cb_spy["edit"][0][2] == "취소했습니다."


def test_callback_push_no_message_id_send_fallback(cb_spy, tmp_path):
    # MEDIUM: message_id 없으면 edit 대신 send 폴백.
    handle_callback(_cq(777, "push", message_id=None), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert len(cb_spy["push"]) == 1
    assert cb_spy["edit"] == []
    assert len(cb_spy["send"]) == 1


def test_callback_unknown_data_ignored(cb_spy, tmp_path):
    # MEDIUM: parse_callback None(알 수 없는 data) → 라우팅 액션 없음(스피너만 종료).
    handle_callback(_cq(777, "bogus"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy["send"] == []
    assert cb_spy["edit"] == []
    assert cb_spy["push"] == []
    assert cb_spy["answer"] == ["cq1"]
