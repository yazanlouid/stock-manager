"""The three demo venues, in the order reset and simulate run them (D87).

Each venue's random seed is the run's seed plus its position here, so the
café, first, keeps the run's own seed.
"""

from app.sim.cafe import CAFE
from app.sim.kitchen import KITCHEN
from app.sim.sweets import SWEETS
from app.sim.venue import Venue

VENUES: tuple[Venue, ...] = (CAFE, SWEETS, KITCHEN)
