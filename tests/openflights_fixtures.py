"""Tiny OpenFlights reference data files for export and review tests, in the format of
OpenFlights' airports.dat and airlines.dat.

The real airport and airline rows are the ones the regression anchor needs plus
the airports the other tests use, so that review's code checks pass for them.
Everything named Fictair, Imaginair, Quillfield and so on is invented. Tests
never download the real files.

Attribution: the real rows (14 airports, 6 airlines) are copied from
OpenFlights' airports.dat and airlines.dat (https://openflights.org/data),
made available under the Open Database License (ODbL) 1.0,
https://opendatacommons.org/licenses/odbl/1-0/.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AIRPORTS = r"""16,"Keflavik International Airport","Keflavik","Iceland","KEF","BIKF",63.985000610352,-22.605600357056,171,0,"N","Atlantic/Reykjavik","airport","OurAirports"
340,"Frankfurt am Main Airport","Frankfurt","Germany","FRA","EDDF",50.033333,8.570556,364,1,"E","Europe/Berlin","airport","OurAirports"
507,"London Heathrow Airport","London","United Kingdom","LHR","EGLL",51.4706,-0.461941,83,0,"E","Europe/London","airport","OurAirports"
580,"Amsterdam Airport Schiphol","Amsterdam","Netherlands","AMS","EHAM",52.308601,4.76389,-11,1,"E","Europe/Amsterdam","airport","OurAirports"
609,"Copenhagen Kastrup Airport","Copenhagen","Denmark","CPH","EKCH",55.617900848389,12.656000137329,17,1,"E","Europe/Copenhagen","airport","OurAirports"
636,"Bergen Airport Flesland","Bergen","Norway","BGO","ENBR",60.29339981,5.218140125,170,1,"E","Europe/Oslo","airport","OurAirports"
644,"Oslo Lufthavn","Oslo","Norway","OSL","ENGM",60.121,11.0502,681,1,"E","Europe/Oslo","airport","OurAirports"
813,"OR Tambo International Airport","Johannesburg","South Africa","JNB","FAOR",-26.1392,28.246,5558,2,"U","Africa/Johannesburg","airport","OurAirports"
1229,"Adolfo Suárez Madrid–Barajas Airport","Madrid","Spain","MAD","LEMD",40.471926,-3.56264,1998,1,"E","Europe/Madrid","airport","OurAirports"
1613,"Vienna International Airport","Vienna","Austria","VIE","LOWW",48.110298156738,16.569700241089,600,1,"E","Europe/Vienna","airport","OurAirports"
1626,"Faro Airport","Faro","Portugal","FAO","LPFR",37.0144004822,-7.96590995789,24,0,"E","Europe/Lisbon","airport","OurAirports"
1638,"Humberto Delgado Airport (Lisbon Portela Airport)","Lisbon","Portugal","LIS","LPPT",38.7813,-9.13592,374,0,"E","Europe/Lisbon","airport","OurAirports"
3448,"General Edward Lawrence Logan International Airport","Boston","United States","BOS","KBOS",42.36429977,-71.00520325,20,-5,"A","America/New_York","airport","OurAirports"
4059,"Jomo Kenyatta International Airport","Nairobi","Kenya","NBO","HKJK",-1.31923997402,36.9277992249,5330,3,"N","Africa/Nairobi","airport","OurAirports"
90001,"Quillfield Aerodrome","Quillfield","Iceland",\N,"BIQQ",64.1,-21.9,50,0,"N","Atlantic/Reykjavik","airport","OurAirports"
"""

AIRLINES = r"""-1,"Unknown",\N,"-","N/A",\N,\N,"Y"
1,"Private flight",\N,"-","N/A","","","Y"
2,"135 Airways",\N,"","GNL","GENERAL","United States","N"
1355,"British Airways",\N,"BA","BAW","SPEEDBIRD","United Kingdom","Y"
2822,"Iberia Airlines",\N,"IB","IBE","IBERIA","Spain","Y"
3126,"Kenya Airways",\N,"KQ","KQA","KENYA","Kenya","Y"
90001,"Fictair",\N,"ZZ","FZZ","FICTAIR","Portugal","Y"
90002,"Aardvark Iberia",\N,"IB","AAI","AARDVARK","Spain","N"
90003,"Zephyr Imaginair",\N,"QQ","ZIQ","ZEPHYR","Iceland","Y"
90004,"Imaginair",\N,"QQ","IMQ","IMAGINAIR","Iceland","Y"
90005,"Defunct Skyways",\N,"WW","DSW","DEFUNCT","Norway","N"
90006,"Ninebird",\N,"9Q","NBQ","NINEBIRD","Norway","Y"
"""

# OpenFlights IDs of the invented rows above, for assertions.
FICTAIR_ID = 90001
IMAGINAIR_ID = 90004
DEFUNCT_ID = 90005
QUILLFIELD_ID = 90001


def write_reference_data(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "airports.dat").write_text(AIRPORTS, encoding="utf-8")
    (directory / "airlines.dat").write_text(AIRLINES, encoding="utf-8")
    return directory


def unreachable_url() -> str:
    """A local address nothing listens on: a download attempt fails at once
    without leaving the machine."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/data/"


@contextmanager
def serve_reference_data(directory: Path):
    """Serve the fixture .dat files over local HTTP; yields the base URL."""
    write_reference_data(directory / "data")
    handler = partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/data/"
    finally:
        server.shutdown()
        server.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass
