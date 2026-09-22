"""Bouwt de sessions-lijst (peer/peering-session/peer-asn/address-family/
prefix-list/route-map/bfd-profile, via REST-joins) - de kern-datastructuur
die zowel generate.py (voor bgpd.conf/peer-groepen) als render_irr_policy.py
(voor het RPSL-beleid) nodig hebben. Gedeeld i.p.v. verdubbeld, zodat beide
scripts altijd exact dezelfde sessiedata zien."""
from __future__ import annotations

from lib.netbox import netbox_get


def has_maintenance_tag(obj: dict | None) -> bool:
    """True als het NetBox-object (peering-session/peer-asn/peering-fabric)
    de tag 'maintenance' (slug) draagt. Cascadeert bewust niet zelf verder -
    de aanroeper combineert dit voor sessie/peer-ASN/fabric-niveau."""
    if not obj:
        return False
    return any(t.get("slug") == "maintenance" for t in (obj.get("tags") or []))


def build_sessions(token: str, scope_id: int) -> list[dict]:
    """Bouwt de sessions-lijst voor de Jinja-context/het IRR-beleid zelf via
    REST-joins (peer/peering-session/peer-asn/address-family/prefix-list/
    route-map/bfd-profile) - equivalent aan wat NetBox's render-config-API
    voorheen leverde (ConfigRenderer._serialize_session in de plugin-
    broncode), maar zonder die vaste/niet-uitbreidbare context te gebruiken."""
    peers = [p for p in netbox_get("/plugins/routing/bgp/peer/?limit=0", token)["results"]
             if p["scope"]["id"] == scope_id]
    peers_by_id = {p["id"]: p for p in peers}

    sessions_raw = [s for s in netbox_get("/plugins/bgp/peering-session/?limit=0", token)["results"]
                    if s["bgp_peer"]["id"] in peers_by_id]

    peerasn_by_asn = {p["asn"]["asn"]: p for p in netbox_get("/plugins/bgp/peer-asn/?limit=0", token)["results"]}
    bfd_profiles = {b["id"]: b for b in netbox_get("/plugins/routing/bfd/profile/?limit=0", token)["results"]}
    prefix_lists = {p["id"]: p["name"] for p in netbox_get("/plugins/routing/objects/prefix-list/?limit=0", token)["results"]}
    route_maps = {r["id"]: r["name"] for r in netbox_get("/plugins/routing/objects/route-map/?limit=0", token)["results"]}
    fabrics_by_id = {f["id"]: f for f in netbox_get("/plugins/bgp/peering-fabric/?limit=0", token)["results"]}

    # assigned_object_type/-id worden door dit endpoint niet als filter
    # ondersteund (geeft altijd ongefilterd alles terug) - client-side
    # filteren op de volledige lijst.
    afs_by_peer: dict[int, list] = {}
    for af in netbox_get("/plugins/routing/bgp/peer-address-family/?limit=0", token)["results"]:
        if af["assigned_object_type"] == "netbox_routing.bgppeer":
            afs_by_peer.setdefault(af["assigned_object_id"], []).append(af)

    sessions = []
    for s in sessions_raw:
        peer = peers_by_id[s["bgp_peer"]["id"]]
        remote_as = peer.get("remote_as")
        if not remote_as:
            continue
        peerasn = peerasn_by_asn.get(remote_as["asn"]) or {}
        bfd = bfd_profiles.get(peer["bfd"]) if peer.get("bfd") else None
        peering_network = s.get("peering_network")
        fabric = fabrics_by_id.get(peering_network["fabric"]["id"]) if peering_network else None

        # Onderhoudsmodus: de tag 'maintenance' (NetBox) op de peering-sessie
        # zelf, het peer-ASN (alle sessies met die peer), of de fabric/IX
        # (alle sessies op die IX) - elk van de drie is voldoende. Vervangt
        # het eerder handmatig per-adresfamilie toewijzen van een lege
        # 'no-import-no-export'-prefixlist: de tag cascadeert vanzelf.
        maintenance = (
            has_maintenance_tag(s)
            or has_maintenance_tag(peerasn)
            or has_maintenance_tag(fabric)
        )

        address_families = []
        for af in afs_by_peer.get(peer["id"], []):
            address_families.append({
                "prefix_list_in": {"name": prefix_lists[af["prefixlist_in"]]} if af.get("prefixlist_in") else None,
                "prefix_list_out": {"name": prefix_lists[af["prefixlist_out"]]} if af.get("prefixlist_out") else None,
                "route_map_in": {"name": route_maps[af["routemap_in"]]} if af.get("routemap_in") else None,
                "route_map_out": {"name": route_maps[af["routemap_out"]]} if af.get("routemap_out") else None,
            })

        sessions.append({
            "name": peer["name"],
            "enabled": peer["enabled"],
            "local_asn": peer["local_as"]["asn"] if peer.get("local_as") else None,
            "peer_asn": remote_as["asn"],
            "peer_name": remote_as.get("description") or f"AS{remote_as['asn']}",
            "irr_as_set": peerasn.get("irr_as_set") or "",
            "ipv4_max_prefixes": peerasn.get("ipv4_max_prefixes"),
            "ipv6_max_prefixes": peerasn.get("ipv6_max_prefixes"),
            "remote_ip": peer["peer"]["address"].split("/")[0],
            "relationship": s["relationship"]["name"] if s.get("relationship") else None,
            "maintenance": maintenance,
            "password": peer.get("password") or "",
            "ttl": peer.get("ttl"),
            "bfd_profile": {
                "name": bfd["name"], "minimum_interval": bfd["min_tx_int"],
                "minimum_rx_interval": bfd["min_rx_int"], "multiplier": bfd["multiplier"],
            } if bfd else None,
            "peering_network": {"fabric": peering_network["fabric"]["name"]} if peering_network else None,
            "address_families": address_families,
        })
    return sessions
