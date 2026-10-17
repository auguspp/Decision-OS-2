"""Read-only room commands on the existing store and authenticated research host."""
from pathlib import Path

from decision_kernel.application.reading_room import ReadingRoom, plan_input
from decision_kernel.domain.records import RecordError, require

OPERATION = "reading-room"
COMMAND = "research-room"


def preflight(operation, payload):
    if operation != OPERATION:
        return
    require(isinstance(payload, dict) and "plan" in payload and set(payload) <= {
        "plan", "public_research_custody_confirmed", "public_feedback_custody_confirmed",
        "public_material_custody_confirmed"},
        "ROOM_PAYLOAD_INVALID")
    plan = plan_input(payload["plan"])
    require(payload.get("public_research_custody_confirmed") is True, "ROOM_PUBLIC_RESEARCH_NOT_CONFIRMED")
    require(plan["agenda"] is None or payload.get("public_feedback_custody_confirmed") is True,
            "ROOM_FEEDBACK_EXPORT_NOT_CONFIRMED")

    require(not (plan.get("materials") or plan.get("review")) or payload.get("public_material_custody_confirmed") is True,
            "ROOM_PUBLIC_MATERIAL_NOT_CONFIRMED")


def perform(store, payload, *, public=True):
    if public:
        preflight(OPERATION, payload)
    from decision_kernel.interfaces.news_rolling_cli import _SourceReads
    reads = _SourceReads(store.read_source)
    result = ReadingRoom(store, source_reader=reads).build(
        payload["plan"], feedback_export=not public or payload.get("public_feedback_custody_confirmed") is True,
        material_export=not public or payload.get("public_material_custody_confirmed") is True)
    result["news_source_reads"] = reads.usage()  # Only the existing Brief/news part, not all room I/O.
    return result


def write_page(path, result):
    from decision_kernel.interfaces.reading_page import render_page
    text = render_page(result)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        raise RecordError("ROOM_OUTPUT_EXISTS_OR_UNWRITABLE") from None


def configure(actions):
    room = actions.add_parser(COMMAND, help="read a fixed catalogue page, original research, agenda and saved Brief")
    room.add_argument("--input", required=True, type=Path, help="reading-room plan JSON; not executable instructions")
    room.add_argument("--out", required=True, type=Path, help="NEW offline HTML path; existing files are never overwritten")


def execute(args, store, load):
    result = perform(store, {"plan": load(args.input)}, public=False)
    write_page(args.out, result)
    return result
