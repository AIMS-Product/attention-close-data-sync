#!/usr/bin/env python3
"""Run only the Avoma first-meeting outcome sync to Close."""

from avoma_to_close_first_meeting_sync import main


if __name__ == "__main__":
    main(outcome_only=True)
