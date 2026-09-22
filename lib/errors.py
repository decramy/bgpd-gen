class BgpdGenError(Exception):
    """Verwachte, nette fout - het aanroepende script stopt met een
    duidelijke boodschap, geen traceback. Gedeeld door alle bgpd-gen-scripts
    zodat lib/cli.py ze op één plek uniform kan afvangen."""
