"""Gem names and colors."""

GEM_COLORS = {
    "Ruby": "#C91D3A",
    "Sapphire": "#0F52BA",
    "Emerald": "#50C878",
    "Amethyst": "#9966CC",
    "Opal": "#DFEAE2",
    "Topaz": "#E6AF54",
    "Garnet": "#7B1E32",
    "Aquamarine": "#7FFFD4",
    "Peridot": "#A7C957",
    "Tourmaline": "#D65A86",
    "Diamond": "#DCECF2",
    "Jade": "#00A86B",
    "Onyx": "#252525",
    "Moonstone": "#CBD9E6",
    "Citrine": "#E4B522",
    "Spinel": "#C72C60",
    "Tanzanite": "#6254A3",
    "Zircon": "#46B9C7",
    "Morganite": "#E9B0A5",
    "Alexandrite": "#477E77",
    "Sunstone": "#DE8650",
    "Labradorite": "#497A88",
    "Turquoise": "#40E0D0",
    "Lapis Lazuli": "#26619C",
    "Malachite": "#218C4E",
    "Agate": "#AE7961",
    "Jasper": "#A94F3D",
    "Chalcedony": "#AEC9DC",
    "Iolite": "#635A9C",
    "Kunzite": "#D8A2CB",
    "Beryl": "#A5D6A7",
    "Fluorite": "#9275BB",
    "Apatite": "#1FA4B4",
    "Obsidian": "#302C35",
    "Rose Quartz": "#F2BFC9",
    "Smoky Quartz": "#88766A",
    "Carnelian": "#BF4C28",
    "Sodalite": "#3B528B",
    "Ametrine": "#AD80AD",
    "Chrysoprase": "#76BD86",
}


def gem_color(name: str) -> str:
    if name in GEM_COLORS:
        return GEM_COLORS[name]
    base, separator, number = name.rpartition(" ")
    if separator and number.isdigit() and int(number) >= 2 and base in GEM_COLORS:
        return GEM_COLORS[base]
    raise ValueError(f"No icon color configured for gem: {name}")
