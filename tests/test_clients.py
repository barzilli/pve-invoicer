from pve_invoicer.clients import Guest, ProxmoxClient
from pve_invoicer.config import ProxmoxConfig


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return {"data": self._data}


class FakeSession:
    def __init__(self, answers):
        self.headers, self.answers, self.calls = {}, answers, []

    def get(self, url, params=None, timeout=None, verify=None):
        self.calls.append((url, params))
        return FakeResponse(self.answers[url.split("/api2/json", 1)[1]])


def test_inventory_and_rrddata_requests():
    session = FakeSession({
        "/cluster/resources": [
            {"id": "qemu/100", "type": "qemu", "node": "pve1", "vmid": 100, "name": "web", "status": "running",
             "maxcpu": 2, "maxmem": 4294967296, "maxdisk": 53687091200},
            {"id": "lxc/101", "type": "lxc", "node": "pve2", "vmid": 101, "name": "db", "status": "stopped",
             "template": 0},
        ],
        "/nodes/pve2/lxc/101/rrddata": [{"time": 1763510400, "maxcpu": 1, "maxmem": 1073741824, "maxdisk": 1}],
    })
    pve = ProxmoxClient(ProxmoxConfig("https://pve:8006", "billing@pve!inv", "secret"), session=session)
    guests = pve.list_guests()
    assert guests == [
        Guest("qemu/100", "pve1", 100, "qemu", "web", False, "running"),
        Guest("lxc/101", "pve2", 101, "lxc", "db", False, "stopped"),
    ]
    assert session.headers["Authorization"] == "PVEAPIToken=billing@pve!inv=secret"
    assert pve.rrddata(guests[1], "hour")[0]["maxcpu"] == 1
    assert session.calls[-1] == ("https://pve:8006/api2/json/nodes/pve2/lxc/101/rrddata",
                                 {"timeframe": "hour", "cf": "AVERAGE"})
