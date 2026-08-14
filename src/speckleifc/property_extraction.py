import math
from typing import Any, Optional, Tuple

from ifcopenshell.entity_instance import entity_instance
from ifcopenshell.util.classification import get_classification, get_references
from ifcopenshell.util.element import get_material, get_type
from ifcopenshell.util.unit import get_full_unit_name, get_project_unit

UNIT_MAPPING = {
    "IfcQuantityLength": "LENGTHUNIT",
    "IfcQuantityArea": "AREAUNIT",
    "IfcQuantityVolume": "VOLUMEUNIT",
    "IfcQuantityCount": None,  # Count quantities have no units
    "IfcQuantityWeight": "MASSUNIT",
    "IfcQuantityTime": "TIMEUNIT",
}

# The quantity kinds worth attributing to a single material, and the key each
# gets in the per-material output.
MATERIAL_QUANTITY_KINDS = {
    "IfcQuantityVolume": "volume",
    "IfcQuantityArea": "area",
    "IfcQuantityLength": "length",
    "IfcQuantityWeight": "weight",
}

# IfcPhysicalComplexQuantity.Discrimination values that mark a breakdown by
# material rather than by something else (an exporter is free to group
# quantities by anything at all). Unset Discrimination is allowed through, since
# plenty of exporters leave it blank.
MATERIAL_QUANTITY_DISCRIMINATIONS = frozenset(
    {"layer", "constituent", "material", "profile"}
)


def extract_properties(element: entity_instance) -> dict[str, object]:
    (psets, qtos) = _get_ifc_object_properties(element)

    properties: dict[str, object] = {
        "Attributes": _get_attributes(element),
        "Property Sets": psets,
    }

    if qtos:
        properties["Quantities"] = qtos

    if classifications := _get_classifications(element):
        properties["Classifications"] = classifications

    if materials := _get_materials(element):
        properties["Materials"] = materials
        properties["Material Quantities"] = _to_material_quantities(materials)

    if (ifc_type := get_type(element)) is not None:
        properties["Element Type Property Sets"] = _get_ifc_element_type_properties(
            ifc_type,
        )
        properties["Element Type Attributes"] = _get_attributes(
            ifc_type,
        )

    return properties


def _get_attributes(element: entity_instance) -> dict[str, object]:
    return element.get_info(True, False, scalar_only=True)


def _get_classification_identification(reference: entity_instance) -> Optional[str]:
    """IFC2X3 calls this ItemReference; IFC4 renamed it to Identification."""
    return getattr(reference, "Identification", None) or getattr(
        reference, "ItemReference", None
    )


def _get_classifications(element: entity_instance) -> dict[str, object]:
    """
    Classification references associated via IfcRelAssociatesClassification.

    Keyed by the owning IfcClassification's name (e.g. "Uniclass 2015"), so a
    consumer that knows which system it cares about can look the code up directly.

    `get_references` already handles inheriting references from the element type
    and letting occurrence-level references override type-level ones per system.

    An element is not supposed to carry more than one reference per system, but
    nothing in the schema prevents it. We keep one entry per system rather than
    nest a list, since the common case is 1:1 and callers look codes up by system
    name. `get_references` returns a set, whose iteration order is incidental
    rather than guaranteed, so sort first: the lowest Identification wins, and
    which one that is does not depend on set internals.

    ReferencedSource is optional, so a reference need not belong to any
    IfcClassification. Those are skipped: with no system there is no stable key
    to file them under, and falling back to the reference's own name invents
    keys that look like systems but are not. Real exports lean on this - one
    ArchiCAD file had 619 IfcSpaces carrying sourceless references for Finnish
    area categories ("Huoneala", "Bruttoala"), which would otherwise have
    surfaced as ~20 spurious classification systems.
    """
    result: dict[str, object] = {}

    references = sorted(
        get_references(element),
        key=lambda r: (
            _get_classification_identification(r) or "",
            r.Name or "",
            r.id(),
        ),
    )

    for reference in references:
        system = get_classification(reference)
        if system is None or not system.Name:
            continue

        # First wins, so the entry is stable when a system appears more than once.
        result.setdefault(
            system.Name,
            {
                "Identification": _get_classification_identification(reference),
                "Name": reference.Name,
                "Location": getattr(reference, "Location", None),
            },
        )

    return result


def _get_materials(element: entity_instance) -> list[dict[str, Any]]:
    """
    The materials assigned to an element, in the order the file declares them.

    Distinct from the render materials the geometry pass collects: those are
    appearance, deduplicated by surface style, and say nothing about what an
    element is actually made of. A wall exported as one grey style is one render
    material but may be four IfcMaterials, and only this side knows that.

    A list rather than a dict keyed by name, for two reasons: layer order is the
    physical build-up of the element and is lost the moment you key by material,
    and one material may legitimately occupy several layers (two gypsum boards
    either side of a stud) which a name key would collapse.

    `get_material` already walks HasAssociations, resolves the LayerSetUsage and
    ProfileSetUsage indirections, and falls back to the element type - the last
    of which matters, since Revit exports routinely put the material on the type
    rather than the occurrence.
    """
    definition = get_material(element, should_skip_usage=True, should_inherit=True)
    if definition is None:
        return []

    stated_quantities = _get_material_quantity_lookup(element)

    materials = []
    for slot in _flatten_material_definition(definition):
        entry = _build_material_entry(element, slot, stated_quantities)
        if entry is not None:
            materials.append(entry)

    return materials


def _flatten_material_definition(definition: entity_instance) -> list[dict[str, Any]]:
    """
    One raw slot per material the definition holds, preserving declared order.

    The set types each name their members differently and carry different extras
    (a layer has a thickness, a constituent has a fraction, a profile has a
    cross-section), so they get a branch each rather than a clever generic walk.

    IFC2X3 lacks Name/Category/Priority on IfcMaterialLayer and Category on
    IfcMaterial, hence `getattr` with a default throughout - the same shim
    `_get_classification_identification` uses for the schema's other renames.
    """
    # get_material(should_skip_usage=True) unwraps these already; belt and
    # braces in case a file associates a usage through some other route.
    for usage_attr in ("ForLayerSet", "ForProfileSet"):
        if (inner := getattr(definition, usage_attr, None)) is not None:
            definition = inner
            break

    if definition.is_a("IfcMaterial"):
        return [{"material": definition}]

    if definition.is_a("IfcMaterialList"):
        return [{"material": material} for material in definition.Materials or []]

    if definition.is_a("IfcMaterialLayerSet"):
        return [
            {
                "material": layer.Material,
                "slotName": getattr(layer, "Name", None),
                "category": getattr(layer, "Category", None),
                "thickness": layer.LayerThickness,
                "priority": getattr(layer, "Priority", None),
                "isVentilated": layer.IsVentilated,
            }
            for layer in definition.MaterialLayers or []
        ]

    if definition.is_a("IfcMaterialConstituentSet"):
        return [
            {
                "material": constituent.Material,
                "slotName": constituent.Name,
                "category": constituent.Category,
                "fraction": constituent.Fraction,
            }
            for constituent in definition.MaterialConstituents or []
        ]

    if definition.is_a("IfcMaterialProfileSet"):
        return [
            {
                "material": profile.Material,
                "slotName": profile.Name,
                "category": profile.Category,
                "priority": profile.Priority,
                "profileName": getattr(profile.Profile, "ProfileName", None),
            }
            for profile in definition.MaterialProfiles or []
        ]

    # An unrecognised IfcMaterialDefinition is skipped rather than raised on,
    # matching how `_get_classifications` treats data it cannot key.
    return []


def _build_material_entry(
    element: entity_instance,
    slot: dict[str, Any],
    stated_quantities: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """
    Resolve one raw slot into its output entry, or None if it cannot be named.

    The material's own name wins over the slot's: `IfcMaterial.Name` is the
    substance ("Concrete C30/37"), while `IfcMaterialLayer.Name` is the role it
    plays in the build-up ("Core"). Consumers are matching against material
    libraries, so the substance is the useful key; the role is kept alongside as
    `layerName` when it says something different.

    A slot with neither is dropped: it cannot key the material quantities dict,
    and a null-named material is not worth an entry. Real files do produce these
    - the IFC fixture in this repo has `IFCMATERIALLAYER($,10.,$,$,$,$,$)`.

    Stated quantities are consumed as they are used, so a material occupying
    several slots carries its quantities on the first of them only. They are
    stated per material, not per slot: repeating a wall's total gypsum volume on
    both of its gypsum layers would double it the moment anyone summed the list.
    """
    material = slot.get("material")
    slot_name = slot.get("slotName")

    name = (material.Name if material is not None else None) or slot_name
    if not name:
        return None

    entry: dict[str, Any] = {"name": name}

    # Same split as the name, and for the same reason: IfcMaterial.Category is
    # the substance class ("concrete", "steel"), while the slot's Category is
    # the function it serves in the build-up ("LoadBearing", "Insulation").
    # Consumers grouping by material want the former.
    slot_category = slot.get("category")
    category = getattr(material, "Category", None) or slot_category
    if category:
        entry["category"] = category

    if slot_category and slot_category != category:
        entry["layerCategory"] = slot_category

    if slot_name and slot_name != name:
        entry["layerName"] = slot_name

    if (thickness := slot.get("thickness")) is not None:
        entry["thickness"] = {
            "value": thickness,
            **_get_project_unit_info(element.file, "LENGTHUNIT"),
        }

    for key in ("fraction", "priority", "isVentilated", "profileName"):
        if (value := slot.get(key)) is not None:
            entry[key] = value

    if material is not None and (psets := _get_material_property_sets(material)):
        entry["Property Sets"] = psets

    entry.update(stated_quantities.pop(name, {}))

    return entry


def _get_material_property_sets(material: entity_instance) -> dict[str, object]:
    """
    IfcMaterialProperties hung off the material - MassDensity and friends, which
    is what turns a volume into a mass downstream.

    Reuses `_get_properties` rather than `ifcopenshell.util.element.get_psets`
    for the reason given on that function: the canonical helper drags in every
    property type, and these are ordinary IfcProperty instances.

    IFC2X3 modelled these as subtypes pointing at the material instead of a
    HasProperties inverse, so this is simply empty there.
    """
    result: dict[str, object] = {}

    for definition in getattr(material, "HasProperties", None) or []:
        if not definition.is_a("IfcMaterialProperties") or not definition.Name:
            continue

        if properties := _get_properties(definition.Properties or []):
            result[definition.Name] = properties

    return result


def _get_material_quantity_lookup(
    element: entity_instance,
) -> dict[str, dict[str, Any]]:
    """
    Quantities the file states per material, keyed by material name.

    The only link IFC gives between a quantity and a single material is an
    IfcPhysicalComplexQuantity whose Name is that material and whose
    Discrimination says the breakdown is by material ("layer", for the usual
    wall and slab case).

    Nothing is derived here. An element's total volume is never split across its
    materials by layer thickness, because that split is a guess: it ignores
    openings resolved per layer, non-prismatic geometry, and layers that do not
    run the full extent of the element. A missing key means the file did not say,
    which a consumer can act on; a plausible-looking number it cannot.

    These same quantities already reach the output raw under "Quantities" - this
    re-keys them by material so they can actually be attributed.
    """
    lookup: dict[str, dict[str, Any]] = {}

    for rel in getattr(element, "IsDefinedBy", []):
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue

        try:
            definition = rel.RelatingPropertyDefinition
            if not definition or not definition.is_a("IfcElementQuantity"):
                continue

            for quantity in definition.Quantities or []:
                if not quantity.is_a("IfcPhysicalComplexQuantity"):
                    continue

                name = quantity.Name
                if not name:
                    continue

                discrimination = (quantity.Discrimination or "").lower()
                if (
                    discrimination
                    and discrimination not in MATERIAL_QUANTITY_DISCRIMINATIONS
                ):
                    continue

                if stated := _get_stated_quantities(quantity, element):
                    lookup.setdefault(name, {}).update(stated)

        except (KeyError, AttributeError):
            # Consistent with `_get_ifc_object_properties`: a malformed quantity
            # set costs its own data, not the whole element's.
            print(f"Skipping {rel}")
            continue

    return lookup


def _get_stated_quantities(
    complex_quantity: entity_instance, element: entity_instance
) -> dict[str, Any]:
    """
    The simple quantities nested in a complex one, keyed by what they measure
    rather than by their authored name, so a consumer does not have to know
    whether this file calls it "NetVolume", "Volume" or "GrossVolume".

    First of each kind wins, which makes the result independent of how many
    variants the exporter wrote.
    """
    result: dict[str, Any] = {}

    for quantity in complex_quantity.HasQuantities or []:
        kind = MATERIAL_QUANTITY_KINDS.get(quantity.is_a())
        if kind is None or kind in result:
            continue

        value = getattr(quantity, quantity.attribute_name(3))

        # Server does not consider `NaN` valid json
        if value is not None and math.isnan(value):
            value = None

        result[kind] = {
            "name": quantity.Name,
            "value": value,
            **_get_unit_info(element, quantity),
        }

    return result


def _to_material_quantities(materials: list[dict[str, Any]]) -> dict[str, Any]:
    """
    The same data in the shape the Revit connector emits, keyed by material name
    with a `materialCategory` alongside the quantities.

    Worth carrying both shapes: this one already has a consumer in
    `specklepy.bundle.eav_extraction._extract_material_quantities`, which flattens
    it to `properties.Material Quantities.{category}.{name}.{kind}` rows, and it
    is what anything written against Revit output will look for. The list keeps
    the layer detail this shape cannot express.

    Several slots can collapse onto one key here - a stud wall boarded both
    sides is two gypsum layers but one gypsum entry. Their quantities are not
    added up: the file states a quantity per material, already covering every
    slot that material occupies, so `_build_material_entry` has put it on one
    slot alone and summing would double it.
    """
    result: dict[str, Any] = {}

    for material in materials:
        entry = result.setdefault(
            material["name"],
            {"materialCategory": material.get("category") or "Unknown"},
        )

        for kind in MATERIAL_QUANTITY_KINDS.values():
            if (quantity := material.get(kind)) is not None:
                entry.setdefault(kind, quantity)

    return result


def _get_ifc_element_type_properties(element: entity_instance) -> dict[str, object]:
    result: dict[str, object] = {}
    for definition in element.HasPropertySets or []:
        if not definition.is_a("IfcPropertySet"):
            continue

        result[definition.Name] = _get_properties(definition.HasProperties)
    return result


def _get_ifc_object_properties(
    element: entity_instance,
) -> Tuple[dict[str, object], dict[str, object]]:
    psets: dict[str, object] = {}
    qtos: dict[str, object] = {}

    for rel in getattr(element, "IsDefinedBy", []):
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue

        definition: entity_instance | None = rel.RelatingPropertyDefinition
        if not definition:
            continue

        try:
            if definition.is_a("IfcPropertySet"):
                set_name = definition.Name
                properties = _get_properties(definition.HasProperties)

                if properties:
                    psets[set_name] = properties

            elif definition.is_a("IfcElementQuantity"):
                quantities_data = _get_quantities(definition.Quantities, element)
                if not quantities_data:
                    continue
                quantities_data["id"] = definition.id()
                qtos[definition.Name] = quantities_data

        except (KeyError, AttributeError):
            # If entity access fails, skip this quantity set
            print(f"Skipping {definition}")
            continue

    return (psets, qtos)


def _get_properties(properties: entity_instance) -> dict[str, Any]:
    """
    There already exists a canonical way to get properties
    `ifcopenshell.util.element.get_properties` but it's very verbose
    and we don't want to bloat our selves with supporting complex property types

    This is a slimmed down version, only supporting a couple of property types
    """
    result: dict[str, Any] = {}

    for prop in properties:
        name = prop.Name
        if prop.is_a("IfcPropertySingleValue"):
            val = prop.NominalValue
            if val is not None:
                result[name] = val.wrappedValue if hasattr(val, "wrappedValue") else val
        elif prop.is_a("IfcPropertyListValue"):
            values = getattr(prop, "ListValues", None)
            if values:
                result[name] = [
                    v.wrappedValue if hasattr(v, "wrappedValue") else v for v in values
                ]
        elif prop.is_a("IfcPropertyEnumeratedValue"):
            values = getattr(prop, "EnumerationValues", None)
            if values:
                result[name] = [
                    v.wrappedValue if hasattr(v, "wrappedValue") else v for v in values
                ]

        # elif prop.is_a("IfcPropertyTableValue"):
        #     properties[name] = #not sure if we want to support these...
    return result


def _get_quantities(
    quantities: list[entity_instance], element: entity_instance
) -> dict[str, Any]:
    """Extract quantity values from IfcPhysicalQuantity entities."""
    results: dict[str, Any] = {}
    for quantity in quantities or []:
        quantity_name = quantity.Name

        if quantity.is_a("IfcPhysicalSimpleQuantity"):
            # Get the quantity value (3rd attribute for simple quantities)
            value = getattr(quantity, quantity.attribute_name(3))
            unit_info = _get_unit_info(element, quantity)

            # Server does not consider `NaN` valid json
            if math.isnan(value):
                value = None

            if unit_info:
                # Create structured quantity object with units
                results[quantity_name] = {
                    "name": quantity_name,
                    "value": value,
                    **unit_info,
                }
            else:
                # No unit info available, keep as simple value with name
                results[quantity_name] = {"name": quantity_name, "value": value}

        elif quantity.is_a("IfcPhysicalComplexQuantity"):
            # Handle complex quantities
            data = {
                k: v
                for k, v in quantity.get_info().items()
                if v is not None and k != "Name"
            }
            data["properties"] = _get_quantities(quantity.HasQuantities, element)
            del data["HasQuantities"]
            results[quantity_name] = data
    return results


def _get_unit_info(
    element: entity_instance, quantity: entity_instance
) -> dict[str, str]:
    """Get unit information for a quantity."""
    # Early return for count quantities - they don't have units
    quantity_type = quantity.is_a()
    if quantity_type == "IfcQuantityCount":
        return {}

    unit = getattr(element, "Unit", None)
    if unit:
        # Quantity has its own unit
        return _format_unit_name(unit)

    else:
        # Fall back to project unit based on quantity type
        unit_type = UNIT_MAPPING.get(quantity_type)
        if not unit_type:
            return {}

        return _get_project_unit_info(element.file, unit_type)


def _get_project_unit_info(ifc_file: Any, unit_type: str) -> dict[str, str]:
    """The project's unit for a measure, for values stated outside a quantity.

    Layer thicknesses are plain length measures in project units, with no
    IfcPhysicalSimpleQuantity to carry a unit of their own.
    """
    try:
        project_unit = get_project_unit(ifc_file, unit_type, use_cache=True)
    except IndexError:
        # get_project_unit reaches for by_type("IfcProject")[0] unguarded. The
        # importer rejects files that do not hold exactly one project, so this
        # is belt and braces - but an absent unit label is not worth failing a
        # conversion over.
        return {}

    if not project_unit:
        return {}

    return _format_unit_name(project_unit)


def _format_unit_name(unit: entity_instance) -> dict[str, str]:
    unit_name = get_full_unit_name(unit)
    formatted_unit_name = unit_name.replace("_", " ").title() if unit_name else ""
    return {"units": formatted_unit_name}
