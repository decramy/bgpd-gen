# bgpd-gen

Genereert OpenBGPD-configuratie voor één router, rechtstreeks vanuit NetBox
(`netbox-peering-manager` + `netbox-routing`). Daarnaast: een wizard om
bilaterale peers toe te voegen, een RPSL-beleidsafdruk voor je IRR-object,
en een script om te verifiëren of een peer RTBH/Flowspec daadwerkelijk
toepast.

## Vereisten

- Python 3.11+, `python3-jinja2`
- `bgpq4` (IRR-prefix-sets)
- `openbgpd` (alleen voor `activate.py`, dat lokaal `bgpd -n`/`bgpctl`
  gebruikt)
- NetBox met de plugins `netbox-peering-manager` en `netbox-routing`, en
  een API-token met lees- en schrijfrechten (zie "NetBox-token" hieronder)

## Scripts

| Script | Doet | Draait |
|---|---|---|
| `generate.py` | NetBox → `output/` (as-sets, IRR-prefix-sets, prefix-lists, `bgpd.conf`) | cron, elk uur |
| `activate.py` | `output/` valideren en live zetten, met automatische rollback | handmatig, als root |
| `add_peer.py` | interactieve wizard: nieuwe bilaterale peer via PeeringDB | handmatig |
| `render_irr_policy.py` | RPSL import/export-beleid printen voor je aut-num-object | handmatig |
| `verify_protection.py` | verifieert of een neighbor RTBH/Flowspec écht toepast | handmatig |

Alle vijf delen code via `lib/`: NetBox REST-client, router/scope/sessie-
lookups, een `bgpctl`-wrapper, en een CLI-bootstrap (logformaat +
foutafhandeling).

## Setup

```sh
git clone <repo> bgpd-gen && cd bgpd-gen
cp config.py.example config.py && $EDITOR config.py
cp netbox-token.example netbox-token && chmod 600 netbox-token && $EDITOR netbox-token
```

`config.py` (niet in git, zie `.gitignore`):

```python
NETBOX_BASE = "https://netbox.example.org/api"
ROUTER_ID = 1        # NetBox BGPRouter-id: <NETBOX_BASE>/plugins/routing/bgp/router/
RELATIONSHIP_IDS = {"transit": 1, "route-server": 2, "bilateral-peering": 3}
# ^ NetBox Relationship-id's: <NETBOX_BASE>/plugins/bgp/relationship/
CANARY_V4 = "203.0.113.1"   # optioneel, alleen voor verify_protection.py
CANARY_V6 = "2001:db8::1"   # optioneel, alleen voor verify_protection.py
```

`RELATIONSHIP_IDS` verwijst naar zelf aangemaakte NetBox `Relationship`-
objecten (geen standaard keuzeveld) - de scripts matchen op id, niet op
naam. `ROUTER_ID` bepaalt via `BGPRouter.assigned_object` ook het Device
(voor `primary_ip4`/router-id en gekoppelde `StaticRoute`s) en via
`BGPRouter.asn` het eigen AS-nummer. Eén `BGPRouter` per router; een Device
met meerdere `BGPRouter`s (bv. voor een gemodelleerde upstream-router) is
geen probleem, want dit script-geheel bedient er precies één.

Voor een tweede router: kopieer de map (incl. `lib/` en `templates/`) en
pas `ROUTER_ID` in die kopie aan.

**NetBox-token**: één token, read+write, in `netbox-token`. Rechten: `view`
op BGP*, BFDProfile, RouteMap, StaticRoute, PeerASN, PrefixList(Entry),
PeeringFabric, PeeringNetwork, Relationship, RIR, IPAddress, Prefix, Device;
`add`+`change` (geen `delete`) op de modellen die `add_peer.py`
aanmaakt/bijwerkt (ASN, IPAddress, PeerASN, BGPPeer, BGPPeerAddressFamily,
PeeringSession, PrefixList).

## Pijplijn (`generate.py generate`)

1. **fetch-as-sets** — `PeerASN.irr_as_set` per bilaterale peer → `as-sets.txt`.
   Beperkt tot Bilateral Peering: transit/route-server-sessies gebruiken
   handmatig beheerde NetBox-prefixlisten (stap 3), geen IRR-macro.
2. **generate-prefix-sets** — `bgpq4` per as-set → `output/peers/as<asn>.conf`,
   macro `$prefix_set_as<asn>_v{4,6}` (op AS-nummer, niet peernaam of as-set).
3. **generate-prefix-lists** — NetBox `PrefixList`/`PrefixListEntry` →
   `prefix-set "naam" { ... }`-blokken in `output/peers/prefix-lists.conf`.
4. **render-main-config** — bouwt de Jinja-context zelf (`build_context()`,
   REST-joins: peer/peering-session/peer-asn/address-family/prefix-list/
   route-map/bfd-profile/static-route) en rendert lokaal `templates/*.j2`
   naar `output/bgpd.conf` + `output/peers/<fabric>.conf`.

Elke stap ook los aanroepbaar (`generate.py fetch-as-sets`, etc.). Resultaat
staat in `output/`, nog niet live.

Lokale templates i.p.v. NetBox's `ConfigTemplate`/render-config-API: die
levert alleen `device`+`sessions`, niet uitbreidbaar zonder de plugin zelf
te patchen. `build_context()` levert de volledige context zelf.

## Configuratie-inhoud

- **IRR-filtering (bilaterale peers)**: `PeerASN.irr_as_set` bepaalt welke
  routes geaccepteerd worden, niet een handmatige prefix-list. Extra's
  (bv. RFC1918) via de lege per-AS `PrefixList` (`as<asn>-extra`),
  additief bovenop het IRR-filter.
- **Max-prefix**: geen cap voor Transit/Route Server - PeeringDB's
  `ipv4_max_prefixes`/`ipv6_max_prefixes` zijn niet representatief voor een
  full-table-sessie. Wel toegepast voor bilaterale peers.
- **Blackhole-community (RFC 7999)**: een route met de well-known
  `BLACKHOLE`-community (`65535:666`) of de informele `<eigen-asn>:666`-
  variant krijgt `nexthop blackhole`, beperkt tot exacte host-routes
  (`/32`/`/128`) zodat een peer geen heel blok kan laten blackholen.
- **Eigen prefixes**: NetBox `StaticRoute` gekoppeld aan het Device →
  `network`-statement + `allow to any prefix` + `roa-set`-entry.
- **Vangnet** (na alle peer-includes, laatste match wint): bogon-prefixes,
  bogon-AS-nummers, max-AS-path-lengte (100), prefixlengte-sanity (v4
  /8–/24, v6 /16–/48), graceful shutdown (RFC 8326 → localpref 0).
- **Connected networks**: interface-IP's van dit Device worden nooit door
  een BGP-geleerde route overschreven (voorkomt fib-update een gedeelde
  peering-LAN-prefix via een gateway laat routeren i.p.v. direct).

## `activate.py`

- Legt vóór wijziging vast welke neighbor-IP's Established zijn
  (`bgpctl -n show summary`, numeriek).
- Valideert `output/bgpd.conf` met `bgpd -n`.
- Bij succes: backup van `/etc/bgpd`, kopiëren, `systemctl reload openbgpd`.
- Na `--wait` seconden (standaard 15): regressiecheck. Alleen een sessie
  die eerst Established was én nog enabled is, maar dat nu niet meer is,
  telt als regressie - sessies die al niet Established waren blijven
  buiten beschouwing.
- Regressie → automatisch terugrollen naar de backup.

Niet door cron aangeroepen: elke wijziging in `output/` zou anders een
onnodige reload triggeren. Draai `activate.py` handmatig na controle, of
koppel het aan een eigen trigger.

Debian's `openbgpd.service` verwacht `/etc/bgpd.conf`; dit script zet daar
een permanente stub (`include "/etc/bgpd/bgpd.conf"`) neer en beheert de
echte config in `/etc/bgpd/`.

## `add_peer.py`

1. Vraagt een AS-nummer, zoekt het op in PeeringDB (net + netixlan).
2. Kruist de gevonden IX'en met de IX'en waar deze router al op zit (live
   uit NetBox: elke `PeeringFabric` met een PeeringDB-koppeling telt mee).
3. Laat je kiezen op welke overlappende IX('en) je wilt peeren.
4. Configureert NetBox: ASN, PeerASN (IRR as-set + max-prefixes uit
   PeeringDB), BGPPeer + BGPPeerAddressFamily per adresfamilie, een lege
   aanvul-`PrefixList`, en een PeeringSession.
5. Optioneel: regenereert de config meteen (`generate.py generate`).

Idempotent per AS+IX (lookup-sleutel is het IP bij die IX, niet de naam).
Nieuwe peers komen als `status=active`/`enabled=True` te staan;
`generate.py`+`activate.py` volstaat om ze live te zetten.

Aanname: RIR-slug `ripe-ncc` voor nieuwe ASN-objecten - pas aan in
`configure_netbox()` als je in een andere regio zit.

## `render_irr_policy.py`

Print het `import:`/`export:`-beleid voor je aut-num-object, afgeleid uit
dezelfde NetBox-data als de bgpd-config:

- Transit/Route Server → `accept ANY`.
- Bilateral Peering met `irr_as_set` → `accept <as-set-naam>`.
- Bilateral Peering zonder `irr_as_set` → `accept AS<remote-asn>`.
- Announce: altijd alleen het eigen AS-nummer.

Print alleen - schrijft niets naar je IRR-database weg.

## `verify_protection.py`

Verifieert of een BGP-neighbor RTBH (RFC 7999, `65535:666`) en/of BGP
Flowspec-regels daadwerkelijk toepast, i.p.v. alleen te vertrouwen dat ze
het ondersteunen.

```sh
python3 verify_protection.py <neighbor-ip> [--rtbh] [--flowspec]
```

**Mechanisme**: ping vanaf een los testadres (`CANARY_V4`/`CANARY_V6` in
`config.py` - eenmalige infrastructuur: loopback-binding + IRR route-object
+ RPKI ROA met max-length 32/128, buiten scope van dit script) naar het
interface-adres van de neighbor. Zet de RTBH-community of een
Flowspec-drop-regel op die canary. Komt de ping niet meer terug, dan is de
neighbor's eigen ICMP-reply weggevallen - geen externe probe nodig.

Flowspec-regels draaien soms als apart ACL dat niet op zelf-gegenereerd
verkeer van de peer's eigen router van toepassing is. Laat de self-reply-
test geen verschil zien, dan zoekt het script automatisch een host áchter
de peer (via `.1`/laatste bruikbare adres van hun eerst-ontvangen
prefixes) en herhaalt de test daarmee.

Eigen AS-nummer komt live uit NetBox (net als `render_irr_policy.py`),
lazy: een `--rtbh`-only run heeft geen NetBox-toegang nodig.

Output: ASCII-checklist (`[OK]`/`[FAIL]`/`[WARN]`/`[SKIP]`), bedoeld om
zonder verdere toelichting door te sturen naar de contactpersoon van de
geteste peer. Preflight controleert eerst of dit systeem OpenBGPD draait en
of de sessie Established is.

**Nog niet gebouwd**: een `--via-route-server`-modus om een peer op een
gedeeld peering-VLAN te testen zonder bilaterale sessie.

## OpenBGPD-syntax: valkuilen (openbgpd-portable 8.8, Debian)

- Filterregels (`allow`/`deny`) horen niet in een `neighbor { }`-blok en
  moeten ná het bijbehorende blok staan, anders: `no such peer`.
- `router-id` moet een letterlijk IPv4-adres zijn, geen hostnaam.
- RTR-sessie: `rtr <adres> { descr "..."; port ...; }`, niet
  `rtr session "naam" { ... }`.
- Een neighbor uitzetten: `down`, niet `disable`/`disable yes`.
- `-`/`><`-operators vereisen spaties (`prefixlen 0 - 32`, niet `0-32`).
- Macronamen: alleen `[a-zA-Z0-9_]`, geen streepjes.
- Een bgpq4-macro gebruik je zónder `prefix-set`-keyword ervoor.
- Een lege `prefix { }`-clausule is ongeldige syntax - macro overslaan als
  bgpq4 niets vond, niet leeg schrijven.
- Een syntaxfout vroeg in een `include`-bestand corrumpeert foutmeldingen
  verderop - los eerst de eerste fout op.
- `prefixlen <getal>` zonder `=` is een syntax error (`prefixlen = 32` wel).
- `community { naam1, naam2 }` (lijst) is geen geldige syntax voor
  `community` - dat werkt alleen bij `AS`/`prefix`. Twee community-
  varianten vereisen dus twee losse `match`-regels.

Test nieuwe syntax lokaal met `bgpd -n` tegen een minimaal bestand; vertrouw
niet blind op de man page (bevat op één punt een voorbeeld dat in de
praktijk niet werkt, zie de `><`-operator hierboven).

## Structuur

```
config.py.example       - template voor config.py (in git)
config.py                - site-config, ingevuld (NIET in git)
netbox-token.example     - template voor netbox-token (in git)
netbox-token             - API-token, ingevuld (chmod 600, NIET in git)

generate.py               - NetBox -> output/ (cron)
activate.py                - output/ valideren en live zetten (handmatig, root)
add_peer.py                  - nieuwe bilaterale peer (handmatig)
render_irr_policy.py          - RPSL-beleid printen (handmatig)
verify_protection.py           - RTBH/Flowspec-peerverificatie (handmatig)

lib/config.py             - config.py + afgeleide bestandslocaties
lib/netbox.py               - NetBox REST-client
lib/router.py                 - router/scope/device/eigen-AS-lookups
lib/sessions.py                 - gedeelde sessions-lijst
lib/bgpctl.py                     - bgpctl-wrapper
lib/util.py, errors.py, cli.py     - subprocess-helper, atomic_write, BgpdGenError, CLI-bootstrap

templates/bgpd.conf.j2    - hoofdconfig-template
templates/peer-group.conf.j2 - peer-groep-template, één render per fabric/IX

netbox-bgpq4-cron         - naar /etc/cron.d/ kopiëren
as-sets.txt               - gegenereerd, NIET in git
output/                   - gegenereerd, NIET in git
generate.log              - cron-output, NIET in git
```

## IRR-macro's in `bgpd.conf`

```
include "peers/as65001.conf"
allow from <ipv4> $prefix_set_as65001_v4
allow from <ipv6> $prefix_set_as65001_v6
```

Geen `prefix-set`-keyword voor de macro - de waarde is zelf al een
`prefix { ... }`-clausule. Automatisch gegenereerd voor elke sessie met
`relationship = "Bilateral Peering"`.

## Bekende beperkingen

- Één router per checkout (`ROUTER_ID`); meerdere routers = meerdere
  checkouts.
- RIR-aanname `ripe-ncc` in `add_peer.py`.
- `verify_protection.py` gaat uit van Linux/`iputils-ping`, geen OpenBSD.
- Geen SAML/RPSL-schrijftoegang; `render_irr_policy.py` print alleen.
