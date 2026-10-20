"""
De recepten van de nagebouwde Tandoor, opgeslagen zoals Tandoor het doet.

Niet als één genest woordenboek per recept, maar in losse tabellen: recepten,
stappen, ingrediënten, foods en eenheden, elk met een eigen id. Dat moet wel,
want daar zit het gedrag waar een client op stukloopt als hij een bestaand
recept bijwerkt. Recipe.steps en Step.ingredients zijn allebei
ManyToManyFields (cookbook/models.py), en Tandoor werkt ze bij met
drf-writable-nested 0.7.2. Uit de broncode van die bibliotheek
(NestedUpdateMixin, update_or_create_reverse_relations,
delete_reverse_relations_if_need) en _get_serializer_for_field:

  * Een geneste stap of ingrediënt mét id wordt bijgewerkt, zonder id nieuw
    gemaakt. Het id wordt opgezocht met Model.objects.filter(pk__in=...),
    zonder te kijken bij welk recept het hoort. Een id van een ander recept
    werkt dus dát object bij en hangt het ook aan dit recept.
  * Wat ontbreekt in de lijst gaat uit de koppeling (m2m.remove), maar het
    object zelf blijft bestaan. Er wordt niets verwijderd.
  * Een bestaand object wordt partial bijgewerkt als de hele aanroep partial
    is (PATCH). Een nieuw object nooit: daar gelden alle verplichte velden,
    ook voor de bestaande ingrediënten die eronder hangen. Een nieuwe stap met
    {"id": 7} als ingrediënt geeft dus "food: This field is required."
  * Een food of eenheid die mét id meekomt in een ingrediënt is een directe
    relatie, en die wordt óók bijgewerkt (update_or_create_direct_relations).
    Wie een ingrediënt verplaatst en daarbij de food meestuurt, schrijft dus
    ook aan die food - en die food hoort bij elk recept dat hem gebruikt.
  * Er is geen transactie om de aanvraag heen (geen ATOMIC_REQUESTS in
    recipes/settings.py). Valt een geneste stap om op validatie, dan zijn de
    stappen ervoor al opgeslagen; alleen de koppeling aan het recept wordt
    niet meer bijgewerkt.

De volgorde is die van de modellen: Step en Ingredient hebben allebei
ordering = ['order', 'pk'].
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

_STAP_VELDEN = ("name", "instruction", "time", "order", "show_as_header",
                "show_ingredients_table")
_INGREDIENT_VELDEN = ("amount", "note", "order", "is_header", "no_amount")


class Fout(Exception):
    """Een DRF-validatiefout: status 400 met de foutenboom als body."""

    def __init__(self, body, status=400):
        super().__init__(str(body))
        self.body = body
        self.status = status


class Opslag:
    def __init__(self):
        self.reset()

    def reset(self):
        self.recepten = {}
        self.stappen = {}
        self.ingredienten = {}
        self.foods = {}
        self.eenheden = {}
        self._ids = {naam: itertools.count(1) for naam in
                     ("recept", "stap", "ingredient", "food", "eenheid")}
        # Hoe vaak een bestaande food of eenheid beschreven werd vanuit een
        # recept. Een brug die alleen indeelt, hoort hier op nul te blijven.
        self.food_schrijfacties = 0
        self._klok = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)

    # -- hulp -------------------------------------------------------------

    def _tijd(self) -> str:
        # Een klok die bij elke schrijfactie verder loopt, zodat updated_at
        # ook bij twee wijzigingen in dezelfde seconde verschilt.
        self._klok += timedelta(seconds=1)
        return self._klok.isoformat()

    def _food(self, data, *, bestaand_bijwerken: bool):
        if data is None:
            return None
        if not isinstance(data, dict):
            raise Fout({"food": ["Invalid data."]})
        if data.get("id") in self.foods:
            food = self.foods[data["id"]]
            if bestaand_bijwerken and "name" in data and data["name"] is not None:
                food["name"] = str(data["name"]).strip()
                self.food_schrijfacties += 1
            return food["id"]
        naam = str(data.get("name") or "").strip()
        if not naam:
            raise Fout({"food": {"name": ["This field is required."]}})
        for food in self.foods.values():           # FoodSerializer.create: get_or_create
            if food["name"] == naam:
                return food["id"]
        nieuw = next(self._ids["food"])
        self.foods[nieuw] = {"id": nieuw, "name": naam}
        return nieuw

    def _eenheid(self, data, *, bestaand_bijwerken: bool):
        if data is None:
            return None
        if not isinstance(data, dict):
            raise Fout({"unit": ["Invalid data."]})
        if data.get("id") in self.eenheden:
            eenheid = self.eenheden[data["id"]]
            if bestaand_bijwerken and "name" in data and data["name"] is not None:
                eenheid["name"] = str(data["name"]).strip()
                self.food_schrijfacties += 1
            return eenheid["id"]
        naam = str(data.get("name") or "").strip()
        if not naam:
            raise Fout({"unit": {"name": ["This field is required."]}})
        for eenheid in self.eenheden.values():
            if eenheid["name"] == naam:
                return eenheid["id"]
        nieuw = next(self._ids["eenheid"])
        self.eenheden[nieuw] = {"id": nieuw, "name": naam}
        return nieuw

    # -- ingrediënten -----------------------------------------------------

    def _maak_ingredient(self, data: dict) -> int:
        for veld in ("food", "unit", "amount"):
            if veld not in data:
                raise Fout({veld: ["This field is required."]})
        if data.get("order", 0) is None:
            raise Fout({"order": ["This field may not be null."]})
        nieuw = next(self._ids["ingredient"])
        self.ingredienten[nieuw] = {
            "id": nieuw,
            "food": self._food(data["food"], bestaand_bijwerken=True),
            "unit": self._eenheid(data["unit"], bestaand_bijwerken=True),
            "amount": float(data["amount"] or 0),
            "note": data.get("note") or "",
            "order": int(data.get("order") or 0),
            "is_header": bool(data.get("is_header", False)),
            "no_amount": bool(data.get("no_amount", False)),
            "original_text": data.get("original_text"),
        }
        return nieuw

    def _werk_ingredient_bij(self, ingredient: dict, data: dict, *, partial: bool):
        if not partial:
            for veld in ("food", "unit", "amount"):
                if veld not in data:
                    raise Fout({veld: ["This field is required."]})
        if "order" in data and data["order"] is None:
            raise Fout({"order": ["This field may not be null."]})
        for veld in _INGREDIENT_VELDEN:
            if veld in data:
                ingredient[veld] = data[veld]
        # IngredientSimpleSerializer.update gooit original_text weg.
        if "food" in data:
            ingredient["food"] = self._food(data["food"], bestaand_bijwerken=True)
        if "unit" in data:
            ingredient["unit"] = self._eenheid(data["unit"], bestaand_bijwerken=True)

    def _ingredienten(self, lijst, *, partial: bool) -> list:
        if not isinstance(lijst, list):
            raise Fout({"ingredients": ['Expected a list of items but got type "dict".']})
        ids, fouten = [], []
        for data in lijst:
            try:
                if not isinstance(data, dict):
                    raise Fout({"non_field_errors": ["Invalid data."]})
                bestaand = self.ingredienten.get(data.get("id"))
                if bestaand:
                    self._werk_ingredient_bij(bestaand, data, partial=partial)
                    ids.append(bestaand["id"])
                else:
                    ids.append(self._maak_ingredient(data))
                fouten.append({})
            except Fout as e:
                fouten.append(e.body)
        if any(fouten):
            raise Fout({"ingredients": fouten})
        return ids

    # -- stappen ----------------------------------------------------------

    def _maak_stap(self, data: dict) -> int:
        if "ingredients" not in data:
            raise Fout({"ingredients": ["This field is required."]})
        # Een nieuwe stap is nooit partial, en dat geldt ook voor de
        # ingrediënten eronder - ook als die al bestaan.
        ingredienten = self._ingredienten(data["ingredients"], partial=False)
        nieuw = next(self._ids["stap"])
        self.stappen[nieuw] = {
            "id": nieuw,
            "name": data.get("name") or "",
            "instruction": data.get("instruction") or "",
            "time": int(data.get("time") or 0),
            "order": int(data.get("order") or 0),
            "show_as_header": bool(data.get("show_as_header", False)),
            "show_ingredients_table": bool(data.get("show_ingredients_table", True)),
            "ingredienten": ingredienten,
        }
        return nieuw

    def _werk_stap_bij(self, stap: dict, data: dict, *, partial: bool):
        if not partial and "ingredients" not in data:
            raise Fout({"ingredients": ["This field is required."]})
        for veld in _STAP_VELDEN:
            if veld in data:
                stap[veld] = data[veld]
        if "ingredients" in data:
            # m2m: wat er staat koppelen, wat ontbreekt loskoppelen.
            stap["ingredienten"] = self._ingredienten(data["ingredients"], partial=partial)

    def _stappen(self, lijst, *, partial: bool) -> list:
        if not isinstance(lijst, list):
            raise Fout({"steps": ['Expected a list of items but got type "dict".']})
        ids, fouten = [], []
        for data in lijst:
            try:
                if not isinstance(data, dict):
                    raise Fout({"non_field_errors": ["Invalid data."]})
                # Opgezocht in álle stappen, niet alleen die van dit recept.
                bestaand = self.stappen.get(data.get("id"))
                if bestaand:
                    self._werk_stap_bij(bestaand, data, partial=partial)
                    ids.append(bestaand["id"])
                else:
                    ids.append(self._maak_stap(data))
                fouten.append({})
            except Fout as e:
                fouten.append(e.body)
        if any(fouten):
            # Wat hierboven al lukte, blijft opgeslagen: er is geen transactie.
            # Alleen de koppeling aan het recept komt er niet meer aan te pas.
            raise Fout({"steps": fouten})
        return ids

    # -- recepten ---------------------------------------------------------

    def maak_recept(self, data: dict) -> int:
        stappen = self._stappen(data.get("steps") or [], partial=False)
        nieuw = next(self._ids["recept"])
        velden = {sleutel: waarde for sleutel, waarde in data.items()
                  if sleutel not in ("id", "steps", "image", "created_by", "created_at",
                                     "updated_at", "food_properties", "image_url")}
        nu = self._tijd()
        self.recepten[nieuw] = {**velden, "id": nieuw, "image": None,
                                "source_url": velden.get("source_url") or "",
                                "created_at": nu, "updated_at": nu, "stappen": stappen}
        return nieuw

    def werk_bij(self, recept_id: int, data: dict, *, partial: bool = True) -> dict:
        recept = self.recepten[recept_id]
        if "steps" in data:
            recept["stappen"] = self._stappen(data["steps"], partial=partial)
        for sleutel, waarde in data.items():
            if sleutel not in ("id", "steps", "image", "created_by", "created_at",
                               "updated_at", "food_properties"):
                recept[sleutel] = waarde
        recept["updated_at"] = self._tijd()
        return self.recept(recept_id)

    def zet_afbeelding(self, recept_id: int, pad: str):
        self.recepten[recept_id]["image"] = pad

    # -- uitvoer zoals RecipeSerializer -----------------------------------

    def _ingredient_uit(self, iid: int) -> dict:
        i = self.ingredienten[iid]
        food = self.foods.get(i["food"])
        eenheid = self.eenheden.get(i["unit"])
        return {
            "id": i["id"],
            "food": dict(food) if food else None,
            "unit": dict(eenheid) if eenheid else None,
            "amount": i["amount"], "conversions": [], "note": i["note"],
            "order": i["order"], "is_header": i["is_header"],
            "no_amount": i["no_amount"], "original_text": i["original_text"],
            "used_in_recipes": [], "checked": False,
        }

    def _stap_uit(self, sid: int) -> dict:
        s = self.stappen[sid]
        ingredienten = sorted(s["ingredienten"],
                              key=lambda i: (self.ingredienten[i]["order"], i))
        return {
            "id": s["id"], "name": s["name"], "instruction": s["instruction"],
            "ingredients": [self._ingredient_uit(i) for i in ingredienten],
            "instructions_markdown": s["instruction"], "time": s["time"],
            "order": s["order"], "show_as_header": s["show_as_header"], "file": None,
            "step_recipe": None, "step_recipe_data": None, "numrecipe": 1,
            "show_ingredients_table": s["show_ingredients_table"],
        }

    def recept(self, recept_id: int) -> dict:
        r = self.recepten[recept_id]
        stappen = sorted(r["stappen"], key=lambda s: (self.stappen[s]["order"], s))
        uit = {sleutel: waarde for sleutel, waarde in r.items() if sleutel != "stappen"}
        uit["steps"] = [self._stap_uit(s) for s in stappen]
        uit.setdefault("keywords", [])
        return uit

    def overzicht(self, recept_id: int) -> dict:
        """RecipeOverviewSerializer: zonder stappen."""
        r = self.recepten[recept_id]
        return {"id": r["id"], "name": r.get("name"), "updated_at": r["updated_at"],
                "image": r.get("image")}

    def tellingen(self) -> dict:
        """Hoeveel er in de hele database staat - ook wat nergens meer aan hangt."""
        gekoppeld = [i for s in self.stappen.values() for i in s["ingredienten"]]
        return {
            "stappen": len(self.stappen),
            "ingredienten": len(self.ingredienten),
            "foods": len(self.foods),
            "food_schrijfacties": self.food_schrijfacties,
            "ingredient_koppelingen": len(gekoppeld),
            "ingredienten_dubbel_gekoppeld": len(gekoppeld) - len(set(gekoppeld)),
        }
