# REPRO-20261127: reproduce B7's part crash_notes from a clean checkout (scripts/reproduce.sh)

The same driver (driver.py), arguments (notes 12 write/after_begin,write/after_effect,write/after_record) and trial offset (11000) as B7's
`codestate_crash_notes.json`, on this checkout's HEAD. Prediction: B7, cell for cell, in every
StateGuard arm. Synthetic fault injection; not production traffic.
