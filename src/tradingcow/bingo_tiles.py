"""Clan bingo board: 24 tiles around a FREE centre (index 12).

(name, difficulty, target, how to prove it)
target > 1 means a team total: every approved submission adds its amount until the target is reached.
"""

POINTS = {"Easy": 1, "Medium": 2, "Hard": 3}
LINE_BONUS = 5
FULL_BONUS = 15
FREE_INDEX = 12

TILES: list[tuple[str, str, int, str]] = [
    ("Tempoross unique", "Easy", 1, "Any unique from the reward pool"),
    ("Wintertodt unique", "Easy", 1, "Any unique from a supply crate"),
    ("100 KBD kills", "Easy", 100, "Team total, show kill count before and after"),
    ("Moons of Peril item", "Easy", 1, "Any armour or weapon piece"),
    ("Any Barrows item", "Easy", 1, "Any Barrows armour or weapon"),

    ("Obor or Bryophyta unique", "Easy", 1, "Hill giant club or Bryophyta's essence"),
    ("Champion scroll", "Easy", 1, "Any champion scroll"),
    ("3 medium clues", "Easy", 3, "Team total, casket screenshots"),
    ("50 Giant Mole kills", "Easy", 50, "Team total, show kill count before and after"),
    ("Scurrius unique", "Easy", 1, "Scurrius' spine or the pet"),

    ("Fire cape", "Medium", 1, "Fire cape drop message"),
    ("Full Barrows set", "Medium", 4, "4 pieces of one brother, team total"),
    ("FREE", "Free", 1, "Free tile"),
    ("Full Moons set", "Medium", 3, "3 pieces of one Moons set, team total"),
    ("Slayer boss unique", "Medium", 1, "Any unique from a slayer boss"),

    ("Dragon impling", "Medium", 1, "Catch one, show the message"),
    ("Zulrah unique", "Medium", 1, "Any Zulrah unique"),
    ("Vorkath head", "Medium", 1, "Vorkath's head drop"),
    ("Wilderness boss unique", "Medium", 1, "Any Wilderness boss unique"),
    ("God Wars unique", "Medium", 1, "Any God Wars Dungeon unique"),

    ("Raid purple", "Hard", 1, "Any purple from CoX, ToB or ToA"),
    ("Any boss pet", "Hard", 1, "Pet drop message"),
    ("Blood shard", "Hard", 1, "From pickpocketing a vyre"),
    ("Dragon warhammer", "Hard", 1, "Lizardman shaman drop"),
    ("Any skilling pet", "Hard", 1, "Pet drop message"),
]

assert len(TILES) == 25 and TILES[FREE_INDEX][0] == "FREE"
