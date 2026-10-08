"""`python -m garmin_intervals_bridge` when the console script is not on PATH."""
import sys

from .cli import main

sys.exit(main())
