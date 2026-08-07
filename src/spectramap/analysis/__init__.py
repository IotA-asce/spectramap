"""Analysis over recorded surveys: summaries, band selection, hotspot estimation."""

from spectramap.analysis.hotspot import HotspotEstimate, estimate_hotspot
from spectramap.analysis.summary import SurveySummary, summarise

__all__ = ["HotspotEstimate", "SurveySummary", "estimate_hotspot", "summarise"]
