# Reply Gates Implementation Notes (2026-07-26)

## Goal
Evidence-first outbound gates without business-keyword primary judgment.

## Module
`G4W/agents/reply_gates.py`
- EvidenceLedger: file_read verified vs clue_only; memory_search clue_only; note_write
- detect_structural_leak / tag_speech_acts / quote_inclusion_check
- gate_final_reply (remediate loop capable)
- sanitize_outbound_reply (controller fail-closed sanitize)
- gate_remember_content (block structural pollution in user memory)

## Wired
- handlers.do_file_read → ledger.add_file_read
- handlers.do_file_write/patch → ledger.note_write
- handlers.do_G4W_memory_search → ledger.add_memory_search
- handlers.do_no_tool → gate_final_reply (remediate via next_prompt)
- controller final extract → sanitize_outbound_reply
- conversation.remember (non-operational) → gate_remember_content

## Constraints
- No business keyword primary judgment
- archive/preview = clue_only
- memory assertions require verified User quotes ⊆ file_read User lines
- commit promises require successful write

## Remaining
- conductor-policy hard examples
- unit tests test_reply_gates.py
- import smoke + pytest
