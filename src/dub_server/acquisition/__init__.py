"""Network-bound acquisition adapters.

Only this package and the API layer are allowed to communicate with indexers,
download clients, and subtitle services.  Inference workers do not import it.
"""

from .factory import build_acquisition_service
from .prowlarr import ProwlarrIndexerGateway
from .qbittorrent import QBittorrentDownloadClient
from .service import AcquisitionService
from .subtitles import CompositeSubtitleProvider, FfprobeSubtitleProbe

__all__ = [
    "AcquisitionService",
    "CompositeSubtitleProvider",
    "FfprobeSubtitleProbe",
    "ProwlarrIndexerGateway",
    "QBittorrentDownloadClient",
    "build_acquisition_service",
]
