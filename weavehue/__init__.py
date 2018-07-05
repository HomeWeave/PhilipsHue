from .service import HueService


__meta__ = {
    "name": "Philips Hue Plugin",
    "class": HueService,
    "deps": ["messaging", "appmanager", "simpledb"],
    "config": [],
}
