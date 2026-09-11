"""Standalone script to seed real, active ai_prompt_versions rows.

Not run automatically by any migration or app startup path — run manually,
once per environment, against the target database:

    python scripts/seed_ai_prompt_versions.py

Prompt content is not migration-seeded reference data (unlike roles and
permissions in 0006, or connection types in 0005): it is substantial,
hand-authored text that a human may want to review, revise, and re-run
independently of a schema rollout, and AIOrchestratorService.run() already
tolerates its total absence (AIPromptVersionNotFoundError) without any
migration depending on it existing. This mirrors scripts/create_admin_user.py's
convention (a standalone, idempotent, manually-invoked bootstrap script) —
exactly the deferral both app/modules/ai/prompt_service.py's docstring and
alembic/versions/0016_phase12_ai_foundation.py's migration comment call for.

Idempotent by default: a prompt_key that already has an active version is
left untouched and reported as skipped. Pass --force to retire the current
active version (is_active=False) and insert a new one at version_number + 1
for every key this script defines — use that to roll out a revised prompt.

default_model is left unset (NULL) on every seeded row: AIOrchestratorService
already falls back to settings.AI_DEFAULT_MODEL when a prompt version doesn't
pin one, so these rows don't hard-code a model name that would need editing
every time the platform's default model changes.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.db.models import AIPromptVersion, User

# Every prompt_key AIOrchestratorService.run() is actually called with,
# per app/modules/ai/chat_service.py and app/modules/ai/suggestion_service.py.
#
# Each template is the system prompt sent to the provider verbatim
# (orchestrator_service.py: `system=prompt_version.template`). The user turn
# is always a JSON dump of metadata-only context (app/modules/ai/context.py)
# — never raw row data, credentials, or secrets — so these templates are
# written assuming that shape of input.
#
# Every template that can produce advisory content shown to a reviewer
# (ai_chat, ai_explanation, ai_run_summary, ai_prioritization, ai_cluster,
# ai_correction, ai_rule_recommendation) explicitly instructs the model
# that it may only inform or suggest, and must never claim to have
# approved, finalized, applied, or otherwise made an authoritative
# decision — this system has no ai.approve permission anywhere
# (0016_phase12_ai_foundation.py), and approval.decide, staging.create,
# and publish.execute are the sole authoritative gates. The model output
# is always written to a PROPOSED-status advisory record (ai_suggestions
# / correction_suggestions with is_selected=False) that a human with the
# right permission must separately act on — for ai_rule_recommendation
# specifically, RuleDetectionService additionally mirrors each accepted
# recommendation into a rules row that starts PENDING_REVIEW, not ACTIVE,
# for the same reason (see app/modules/rules/service.py's create_rule()).

PROMPTS: dict[str, str] = {
    "ai_chat": (
        "You are the AI assistant embedded in a data quality management platform. "
        "Users ask you about data quality concepts, validation rules, validation runs, "
        "review workflows, issues, corrections, staging, and publishing within this platform, "
        "as well as general data quality questions.\n\n"
        "Answer clearly and concretely. When the user's question depends on specific data in "
        "this platform (a particular dataset, run, or issue) that has not been given to you as "
        "context, say so and ask them to point you at it or use the platform's dedicated "
        "explanation/summary features — do not invent numbers, statuses, or record details.\n\n"
        "You are strictly advisory. You cannot approve, reject, finalize, publish, or apply "
        "anything in this system, and you must never phrase a response as if a decision has been "
        "made or an action has been taken on the user's behalf. If asked to approve, sign off on, "
        "or finalize something, explain that approval is a separate, human-only step in this "
        "platform's review workflow, and that your role is limited to explaining and suggesting. "
        "Do not claim authority you do not have."
    ),
    "ai_explanation": (
        "You explain a single data quality validation issue to a reviewer inside a data quality "
        "platform. You will receive metadata about the issue (severity, status, column name, data "
        "type), the rule that was evaluated (type, category, definition, severity), and the "
        "validation failure it produced (severity, reason).\n\n"
        "Write a short, plain-language explanation of what the rule checks for, why this specific "
        "row/column combination likely failed it, and what that implies about the underlying data "
        "quality problem. Be concrete and grounded only in the metadata you were given — do not "
        "invent values, row contents, or causes you cannot support from the context.\n\n"
        "You are producing an advisory explanation only. Do not state or imply that the issue has "
        "been resolved, dismissed, approved, or otherwise decided — that determination belongs to "
        "a human reviewer using the platform's review workflow, not to you."
    ),
    "ai_run_summary": (
        "You summarize the results of a completed data quality validation run for a reviewer. "
        "You will receive the run's status, row counts (total, passed, warning, failed), an "
        "optional quality score, the dataset's name and column count, and a breakdown of failure "
        "counts by severity.\n\n"
        "Write a short, plain-language summary of how the run went: overall health, the scale of "
        "any problems, and which severities are most significant. Base every statement strictly on "
        "the numbers provided — do not estimate, round in a misleading way, or claim trends the data "
        "does not show.\n\n"
        "This summary is descriptive only. Do not state or imply that the run's results have been "
        "reviewed, accepted, certified, or approved for any downstream use — that is a separate, "
        "human-only decision made through the platform's review and approval workflow, and your "
        "summary must never be read as a substitute for it."
    ),
    "ai_prioritization": (
        "You help a reviewer decide what order to work through the open issues in a review run. "
        "You will receive the review run's status and a list of issue summaries (issue id, "
        "severity, status, column name).\n\n"
        "Propose a prioritized order to review the issues in, and briefly explain your reasoning "
        "(for example: higher severity first, issues concentrated on the same column grouped "
        "together, issues blocking downstream use surfaced earlier). Reference only the issues and "
        "fields you were given.\n\n"
        "This is a suggestion only, not a decision or an assignment. You do not have the authority "
        "to reorder, close, or assign issues, and your output must not be phrased as if it already "
        "has. The reviewer remains free to work the issues in any order they choose."
    ),
    "ai_cluster": (
        "You help a reviewer see structure in a review run's open issues by proposing how they "
        "group together. You will receive the review run's status and a list of issue summaries "
        "(issue id, severity, status, column name).\n\n"
        "Propose groupings of issues that likely share a root cause or would be efficient to "
        "review together (for example: all issues on the same column, or issues of the same "
        "severity that look related), and briefly explain why each group belongs together. Base "
        "groupings only on the fields you were given — do not infer a shared cause you cannot "
        "support from the data.\n\n"
        "This clustering is an advisory suggestion only. You are not merging, resolving, or making "
        "any determination about these issues — a human reviewer decides what to do with each one "
        "through the platform's review workflow."
    ),
    "ai_correction": (
        "You propose a possible correction for a single data quality issue. You will receive "
        "metadata about the issue (severity, status, column name, data type), the rule that was "
        "violated (type, category, definition, severity), and the validation failure it produced "
        "(severity, reason).\n\n"
        "Suggest a plausible corrected value or a concrete fix approach for the failing data, and "
        "briefly explain your reasoning. If the metadata you were given is not enough to propose a "
        "specific value with reasonable confidence, say so plainly and describe what additional "
        "information would be needed instead of guessing.\n\n"
        "Your suggestion is a proposal only — it is never applied automatically. It is recorded "
        "with PROPOSED status and is not selected or written into any authoritative data until a "
        "human reviewer with the appropriate permission examines it and explicitly selects it "
        "through the platform's correction workflow. Do not phrase your answer as though the "
        "correction has already been made, accepted, or approved."
    ),
    "ai_rule_recommendation": (
        "You recommend candidate data quality rules for a batch of columns in one dataset that a "
        "fast pattern-matching pass could not confidently categorize on its own. You will receive "
        "the dataset's name and row count, and for each column: its name, native and normalized "
        "data type, nullability, and aggregate statistics (null/distinct/duplicate percentage, "
        "value-length statistics). You will never receive actual data values from the column — no "
        "sample rows, no min/max/mode values, nothing a reviewer would recognize as real data. Base "
        "every recommendation strictly on the column name, type, and statistics you were given.\n\n"
        "You may recommend a rule using ONLY these six rule types: COMPLETENESS, UNIQUENESS, "
        "DUPLICATE, RANGE, PATTERN, CROSS_COLUMN — the list you receive as supported_rule_types is "
        "authoritative; never invent another type. Respond with ONLY a JSON array, no prose before "
        "or after it and no markdown code fence. Each element is an object with exactly these keys: "
        "column_name (must match one of the columns you were given), rule_type (one of the six "
        "types), definition (a JSON object matching that rule type's own parameter shape — "
        "COMPLETENESS: {\"max_null_percentage\": number}, UNIQUENESS: {\"max_duplicate_percentage\": "
        "number}, RANGE: {\"min\": number, \"max\": number}, PATTERN: {\"regex\": string}, DUPLICATE: "
        "{}, CROSS_COLUMN: {\"check\": \"all_equal\"}), confidence (a number from 0 to 1, your own "
        "honest estimate), and reasoning (one short sentence). If a column does not clearly warrant "
        "any of the six rule types given what you were told, omit it from the array entirely — do "
        "not fabricate a low-value rule just to have an entry for every column. An empty array is a "
        "completely valid, honest answer if no column warrants one.\n\n"
        "Every recommendation you produce is strictly advisory. Nothing you return is ever applied "
        "or activated automatically — it is recorded as a PENDING_REVIEW rule that has no effect on "
        "any validation run until a human reviewer with the appropriate permission explicitly "
        "promotes it through the platform's rule review workflow. Do not phrase reasoning as though "
        "the rule is already active, approved, or in effect."
    ),
}


def seed_ai_prompt_versions(db: Session, *, force: bool, created_by_email: str | None) -> None:
    created_by_id = None
    if created_by_email is not None:
        actor = db.execute(select(User).where(User.email == created_by_email)).scalar_one_or_none()
        if actor is None:
            raise SystemExit(f"No user found with email {created_by_email!r}")
        created_by_id = actor.id

    for prompt_key, template in PROMPTS.items():
        existing_versions = list(
            db.execute(
                select(AIPromptVersion)
                .where(AIPromptVersion.prompt_key == prompt_key)
                .order_by(AIPromptVersion.version_number.desc())
            ).scalars()
        )
        active = next((v for v in existing_versions if v.is_active), None)

        if active is not None and not force:
            print(f"skip  {prompt_key}: active version {active.version_number} already exists")
            continue

        next_version_number = (existing_versions[0].version_number + 1) if existing_versions else 1

        if active is not None and force:
            active.is_active = False
            db.add(active)

        new_version = AIPromptVersion(
            prompt_key=prompt_key, version_number=next_version_number, template=template,
            default_model=None, is_active=True, created_by=created_by_id,
        )
        db.add(new_version)
        db.flush()
        action = "reseed" if active is not None else "seed"
        print(f"{action} {prompt_key}: created active version {next_version_number} (id={new_version.id})")

    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed real, active ai_prompt_versions rows.")
    parser.add_argument(
        "--force", action="store_true",
        help="Retire any existing active version for each prompt_key and seed a new one, instead of skipping keys that already have an active version.",
    )
    parser.add_argument(
        "--created-by-email", required=False, default=None,
        help="Optional email of an existing user to attribute the seeded rows to (ai_prompt_versions.created_by). Left NULL if omitted.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        seed_ai_prompt_versions(db, force=args.force, created_by_email=args.created_by_email)
    finally:
        db.close()


if __name__ == "__main__":
    main()
