import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")

from speckleifc.property_extraction import extract_properties  # noqa: E402


def _guid() -> str:
    return ifcopenshell.guid.new()


@pytest.fixture
def ifc_file():
    return ifcopenshell.file(schema="IFC4")


def _add_uniclass(file, identification="Ss_25_10_30"):
    """A hierarchical Uniclass reference chain, as real files encode it."""
    system = file.create_entity(
        "IfcClassification", Source="NBS", Edition="2015", Name="Uniclass 2015"
    )
    parent = file.create_entity(
        "IfcClassificationReference",
        Identification="Ss_25",
        Name="Wall and barrier systems",
        ReferencedSource=system,
    )
    return file.create_entity(
        "IfcClassificationReference",
        Location="https://uniclass.thenbs.com/Ss_25_10_30",
        Identification=identification,
        Name="Cast in situ concrete wall systems",
        ReferencedSource=parent,
    )


def _associate(file, element, reference):
    file.create_entity(
        "IfcRelAssociatesClassification",
        GlobalId=_guid(),
        RelatedObjects=[element],
        RelatingClassification=reference,
    )


def test_extracts_classification_keyed_by_system(ifc_file):
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    _associate(ifc_file, wall, _add_uniclass(ifc_file))

    classifications = extract_properties(wall)["Classifications"]

    assert classifications == {
        "Uniclass 2015": {
            "Identification": "Ss_25_10_30",
            "Name": "Cast in situ concrete wall systems",
            "Location": "https://uniclass.thenbs.com/Ss_25_10_30",
        }
    }


def test_extracts_multiple_systems(ifc_file):
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    _associate(ifc_file, wall, _add_uniclass(ifc_file))

    ccs = ifc_file.create_entity("IfcClassification", Source="Molio", Name="CCS")
    _associate(
        ifc_file,
        wall,
        ifc_file.create_entity(
            "IfcClassificationReference",
            Identification="BA",
            Name="Groundworks structure",
            ReferencedSource=ccs,
        ),
    )

    classifications = extract_properties(wall)["Classifications"]

    assert set(classifications) == {"Uniclass 2015", "CCS"}
    assert classifications["CCS"]["Identification"] == "BA"


def test_inherits_classification_from_element_type(ifc_file):
    slab_type = ifc_file.create_entity(
        "IfcSlabType", GlobalId=_guid(), Name="SL_Concrete220"
    )
    slab = ifc_file.create_entity("IfcSlab", GlobalId=_guid(), Name="Slab-001")
    ifc_file.create_entity(
        "IfcRelDefinesByType",
        GlobalId=_guid(),
        RelatedObjects=[slab],
        RelatingType=slab_type,
    )
    _associate(ifc_file, slab_type, _add_uniclass(ifc_file))

    classifications = extract_properties(slab)["Classifications"]

    assert classifications["Uniclass 2015"]["Identification"] == "Ss_25_10_30"


def test_unclassified_element_has_no_classifications_key(ifc_file):
    beam = ifc_file.create_entity("IfcBeam", GlobalId=_guid(), Name="Beam-001")

    assert "Classifications" not in extract_properties(beam)


def _sourceless_reference(file):
    """
    ReferencedSource is optional. A real ArchiCAD export used sourceless
    references for Finnish area categories on IfcSpace - not classification
    systems at all.
    """
    return file.create_entity(
        "IfcClassificationReference",
        Identification="9350",
        Name="Huoneala",
        ReferencedSource=None,
    )


def test_reference_without_a_source_system_is_skipped(ifc_file):
    space = ifc_file.create_entity("IfcSpace", GlobalId=_guid(), Name="Space-001")
    _associate(ifc_file, space, _sourceless_reference(ifc_file))

    assert "Classifications" not in extract_properties(space)


def test_sourceless_reference_does_not_displace_a_real_system(ifc_file):
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    _associate(ifc_file, wall, _add_uniclass(ifc_file))
    _associate(ifc_file, wall, _sourceless_reference(ifc_file))

    assert set(extract_properties(wall)["Classifications"]) == {"Uniclass 2015"}


@pytest.mark.parametrize(
    "association_order",
    [("Ss_25_10_30", "Ss_25_10_95"), ("Ss_25_10_95", "Ss_25_10_30")],
)
def test_duplicate_system_keeps_lowest_identification(ifc_file, association_order):
    """
    One entry per system, and which reference wins does not depend on the order
    they were associated in (`get_references` returns a set, whose iteration
    order is incidental rather than guaranteed).
    """
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    for identification in association_order:
        _associate(ifc_file, wall, _add_uniclass(ifc_file, identification))

    classifications = extract_properties(wall)["Classifications"]

    assert classifications["Uniclass 2015"]["Identification"] == "Ss_25_10_30"


def test_leaves_existing_property_extraction_untouched(ifc_file):
    """Classifications are additive: nothing else about the payload changes."""
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    before = set(extract_properties(wall))

    _associate(ifc_file, wall, _add_uniclass(ifc_file))

    assert set(extract_properties(wall)) == before | {"Classifications"}


# ───────────────────────────────── materials ──────────────────────────────────


def _set_project_units(file):
    """Quantity and thickness unit labels are read off the project."""
    units = [
        file.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE"),
        file.create_entity("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE"),
        file.create_entity("IfcSIUnit", UnitType="VOLUMEUNIT", Name="CUBIC_METRE"),
    ]
    file.create_entity(
        "IfcProject",
        GlobalId=_guid(),
        Name="Project",
        UnitsInContext=file.create_entity("IfcUnitAssignment", Units=units),
    )


def _associate_material(file, element, material_definition):
    file.create_entity(
        "IfcRelAssociatesMaterial",
        GlobalId=_guid(),
        RelatedObjects=[element],
        RelatingMaterial=material_definition,
    )


def _add_layer_set(file, *layers, usage=True):
    """Layers arrive as (material, thickness, name) triples, in build-up order."""
    layer_set = file.create_entity(
        "IfcMaterialLayerSet",
        MaterialLayers=[
            file.create_entity(
                "IfcMaterialLayer",
                Material=material,
                LayerThickness=thickness,
                Name=name,
            )
            for material, thickness, name in layers
        ],
        LayerSetName="Build-up",
    )
    if not usage:
        return layer_set

    return file.create_entity(
        "IfcMaterialLayerSetUsage",
        ForLayerSet=layer_set,
        LayerSetDirection="AXIS2",
        DirectionSense="POSITIVE",
        OffsetFromReferenceLine=0.0,
    )


def _add_complex_quantity(file, element, *, name, volume=None, area=None, kind="layer"):
    quantities = []
    if volume is not None:
        quantities.append(
            file.create_entity(
                "IfcQuantityVolume", Name="NetVolume", VolumeValue=volume
            )
        )
    if area is not None:
        quantities.append(
            file.create_entity("IfcQuantityArea", Name="NetSideArea", AreaValue=area)
        )

    complex_quantity = file.create_entity(
        "IfcPhysicalComplexQuantity",
        Name=name,
        Discrimination=kind,
        HasQuantities=quantities,
    )
    file.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=_guid(),
        RelatedObjects=[element],
        RelatingPropertyDefinition=file.create_entity(
            "IfcElementQuantity",
            GlobalId=_guid(),
            Name="BaseQuantities",
            Quantities=[complex_quantity],
        ),
    )


def test_extracts_a_single_material(ifc_file):
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    _associate_material(
        ifc_file,
        wall,
        ifc_file.create_entity("IfcMaterial", Name="Brick", Category="Masonry"),
    )

    properties = extract_properties(wall)

    assert properties["Materials"] == [{"name": "Brick", "category": "Masonry"}]
    assert properties["Material Quantities"] == {
        "Brick": {"materialCategory": "Masonry"}
    }


def test_extracts_layers_in_build_up_order_with_thicknesses(ifc_file):
    """
    Layer order is the physical build-up of the element, which is why materials
    are a list. A dict keyed by material would lose it.
    """
    _set_project_units(ifc_file)
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    concrete = ifc_file.create_entity(
        "IfcMaterial", Name="Concrete", Category="Concrete"
    )
    wool = ifc_file.create_entity("IfcMaterial", Name="Mineral Wool")
    _associate_material(
        ifc_file,
        wall,
        _add_layer_set(ifc_file, (concrete, 0.24, "Core"), (wool, 0.06, "Insulation")),
    )

    materials = extract_properties(wall)["Materials"]

    assert [material["name"] for material in materials] == ["Concrete", "Mineral Wool"]
    assert materials[0]["thickness"] == {"value": 0.24, "units": "Metre"}
    assert materials[1]["thickness"] == {"value": 0.06, "units": "Metre"}
    assert materials[0]["layerName"] == "Core"


def test_extracts_constituent_fractions(ifc_file):
    column = ifc_file.create_entity("IfcColumn", GlobalId=_guid(), Name="Col-001")
    constituent_set = ifc_file.create_entity(
        "IfcMaterialConstituentSet",
        Name="Reinforced concrete",
        MaterialConstituents=[
            ifc_file.create_entity(
                "IfcMaterialConstituent",
                Name="Body",
                Material=ifc_file.create_entity("IfcMaterial", Name="Concrete"),
                Fraction=0.98,
            ),
            ifc_file.create_entity(
                "IfcMaterialConstituent",
                Name="Rebar",
                Material=ifc_file.create_entity("IfcMaterial", Name="Steel"),
                Fraction=0.02,
            ),
        ],
    )
    _associate_material(ifc_file, column, constituent_set)

    materials = extract_properties(column)["Materials"]

    assert [(m["name"], m["fraction"]) for m in materials] == [
        ("Concrete", 0.98),
        ("Steel", 0.02),
    ]


def test_extracts_profile_set(ifc_file):
    beam = ifc_file.create_entity("IfcBeam", GlobalId=_guid(), Name="Beam-001")
    profile_set = ifc_file.create_entity(
        "IfcMaterialProfileSet",
        Name="IPE300",
        MaterialProfiles=[
            ifc_file.create_entity(
                "IfcMaterialProfile",
                Name="Section",
                Material=ifc_file.create_entity("IfcMaterial", Name="Steel S355"),
                Profile=ifc_file.create_entity(
                    "IfcRectangleProfileDef",
                    ProfileType="AREA",
                    ProfileName="IPE300",
                    XDim=0.15,
                    YDim=0.3,
                ),
            )
        ],
    )
    _associate_material(ifc_file, beam, profile_set)

    (material,) = extract_properties(beam)["Materials"]

    assert material["name"] == "Steel S355"
    assert material["profileName"] == "IPE300"


def test_extracts_material_list(ifc_file):
    proxy = ifc_file.create_entity(
        "IfcBuildingElementProxy", GlobalId=_guid(), Name="Proxy-001"
    )
    _associate_material(
        ifc_file,
        proxy,
        ifc_file.create_entity(
            "IfcMaterialList",
            Materials=[
                ifc_file.create_entity("IfcMaterial", Name="Glass"),
                ifc_file.create_entity("IfcMaterial", Name="Aluminium"),
            ],
        ),
    )

    materials = extract_properties(proxy)["Materials"]

    assert [material["name"] for material in materials] == ["Glass", "Aluminium"]


def test_inherits_material_from_element_type(ifc_file):
    """Revit exports routinely put the layer set on the type, not the occurrence."""
    wall_type = ifc_file.create_entity(
        "IfcWallType", GlobalId=_guid(), Name="WA_Ext", PredefinedType="STANDARD"
    )
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    ifc_file.create_entity(
        "IfcRelDefinesByType",
        GlobalId=_guid(),
        RelatedObjects=[wall],
        RelatingType=wall_type,
    )
    concrete = ifc_file.create_entity("IfcMaterial", Name="Concrete")
    _associate_material(
        ifc_file,
        wall_type,
        _add_layer_set(ifc_file, (concrete, 0.2, None), usage=False),
    )

    assert [m["name"] for m in extract_properties(wall)["Materials"]] == ["Concrete"]


def test_extracts_stated_per_material_quantities(ifc_file):
    _set_project_units(ifc_file)
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    concrete = ifc_file.create_entity(
        "IfcMaterial", Name="Concrete", Category="Concrete"
    )
    _associate_material(
        ifc_file, wall, _add_layer_set(ifc_file, (concrete, 0.24, "Core"))
    )
    _add_complex_quantity(ifc_file, wall, name="Concrete", volume=3.12, area=13.0)

    properties = extract_properties(wall)

    assert properties["Material Quantities"] == {
        "Concrete": {
            "materialCategory": "Concrete",
            "volume": {"name": "NetVolume", "value": 3.12, "units": "Cubic Metre"},
            "area": {"name": "NetSideArea", "value": 13.0, "units": "Square Metre"},
        }
    }


def test_omits_quantities_the_file_does_not_state(ifc_file):
    """
    The read-only guarantee. An element total is never split across its
    materials by thickness - that split ignores openings resolved per layer and
    layers that do not run the element's full extent. A missing key is
    actionable; a plausible-looking guess is not.
    """
    _set_project_units(ifc_file)
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    concrete = ifc_file.create_entity("IfcMaterial", Name="Concrete")
    wool = ifc_file.create_entity("IfcMaterial", Name="Mineral Wool")
    _associate_material(
        ifc_file,
        wall,
        _add_layer_set(ifc_file, (concrete, 0.24, None), (wool, 0.06, None)),
    )
    # A whole-element total, with no per-material breakdown.
    ifc_file.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=_guid(),
        RelatedObjects=[wall],
        RelatingPropertyDefinition=ifc_file.create_entity(
            "IfcElementQuantity",
            GlobalId=_guid(),
            Name="Qto_WallBaseQuantities",
            Quantities=[
                ifc_file.create_entity(
                    "IfcQuantityVolume", Name="NetVolume", VolumeValue=12.0
                )
            ],
        ),
    )

    quantities = extract_properties(wall)["Material Quantities"]

    assert quantities == {
        "Concrete": {"materialCategory": "Unknown"},
        "Mineral Wool": {"materialCategory": "Unknown"},
    }


def test_ignores_complex_quantities_not_broken_down_by_material(ifc_file):
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    concrete = ifc_file.create_entity("IfcMaterial", Name="Concrete")
    _associate_material(
        ifc_file, wall, _add_layer_set(ifc_file, (concrete, 0.24, None))
    )
    _add_complex_quantity(ifc_file, wall, name="Concrete", volume=3.12, kind="storey")

    assert "volume" not in extract_properties(wall)["Material Quantities"]["Concrete"]


def test_repeated_material_is_not_double_counted(ifc_file):
    """
    A stud wall boarded both sides is two gypsum layers but one stated gypsum
    volume, covering both. The list keeps the layers apart, the name-keyed Revit
    shape cannot - and neither may report more gypsum than the file states.
    """
    _set_project_units(ifc_file)
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    gypsum = ifc_file.create_entity("IfcMaterial", Name="Gypsum")
    _associate_material(
        ifc_file,
        wall,
        _add_layer_set(ifc_file, (gypsum, 0.0125, "Inner"), (gypsum, 0.0125, "Outer")),
    )
    _add_complex_quantity(ifc_file, wall, name="Gypsum", volume=0.5)

    properties = extract_properties(wall)
    materials = properties["Materials"]

    assert [m["layerName"] for m in materials] == ["Inner", "Outer"]
    assert properties["Material Quantities"]["Gypsum"]["volume"]["value"] == 0.5
    # Carried once, so summing the list does not inflate the total either.
    assert sum(m["volume"]["value"] for m in materials if "volume" in m) == 0.5


def test_layer_without_a_material_falls_back_to_the_layer_name(ifc_file):
    """
    The IFC fixture in this repo contains exactly this:
    `#44=IFCMATERIALLAYER($,10.,$,$,$,$,$)` - a layer set whose only layer has
    no material at all.
    """
    _set_project_units(ifc_file)
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    _associate_material(ifc_file, wall, _add_layer_set(ifc_file, (None, 0.1, "Screed")))

    (material,) = extract_properties(wall)["Materials"]

    assert material["name"] == "Screed"
    assert "layerName" not in material


def test_nameless_layer_is_skipped(ifc_file):
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    concrete = ifc_file.create_entity("IfcMaterial", Name="Concrete")
    _associate_material(
        ifc_file,
        wall,
        _add_layer_set(ifc_file, (None, 0.1, None), (concrete, 0.2, None)),
    )

    assert [m["name"] for m in extract_properties(wall)["Materials"]] == ["Concrete"]


def test_extracts_material_property_sets(ifc_file):
    """Density is what turns an extracted volume into a mass downstream."""
    slab = ifc_file.create_entity("IfcSlab", GlobalId=_guid(), Name="Slab-001")
    concrete = ifc_file.create_entity("IfcMaterial", Name="Concrete C30/37")
    ifc_file.create_entity(
        "IfcMaterialProperties",
        Name="Pset_MaterialCommon",
        Material=concrete,
        Properties=[
            ifc_file.create_entity(
                "IfcPropertySingleValue",
                Name="MassDensity",
                NominalValue=ifc_file.create_entity("IfcMassDensityMeasure", 2400.0),
            )
        ],
    )
    _associate_material(ifc_file, slab, concrete)

    (material,) = extract_properties(slab)["Materials"]

    assert material["Property Sets"] == {"Pset_MaterialCommon": {"MassDensity": 2400.0}}


def test_category_prefers_the_substance_over_the_layer_function(ifc_file):
    """
    IfcMaterial.Category is what the material is ("concrete"); the layer's
    Category is the job it does ("LoadBearing"). Consumers grouping by material
    want the former, so the latter is kept separately.
    """
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    concrete = ifc_file.create_entity(
        "IfcMaterial", Name="Concrete", Category="concrete"
    )
    layer_set = ifc_file.create_entity(
        "IfcMaterialLayerSet",
        MaterialLayers=[
            ifc_file.create_entity(
                "IfcMaterialLayer",
                Material=concrete,
                LayerThickness=0.24,
                Category="LoadBearing",
            )
        ],
    )
    _associate_material(ifc_file, wall, layer_set)

    (material,) = extract_properties(wall)["Materials"]

    assert material["category"] == "concrete"
    assert material["layerCategory"] == "LoadBearing"


def test_element_without_a_material_has_no_material_keys(ifc_file):
    beam = ifc_file.create_entity("IfcBeam", GlobalId=_guid(), Name="Beam-001")

    properties = extract_properties(beam)

    assert "Materials" not in properties
    assert "Material Quantities" not in properties


def test_materials_leave_existing_property_extraction_untouched(ifc_file):
    """Materials are additive: nothing else about the payload changes."""
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    before = set(extract_properties(wall))

    _associate_material(
        ifc_file, wall, ifc_file.create_entity("IfcMaterial", Name="Brick")
    )

    assert set(extract_properties(wall)) == before | {
        "Materials",
        "Material Quantities",
    }


def test_handles_ifc2x3_layers_without_name_or_category():
    """
    IfcMaterialLayer gained Name, Category and Priority in IFC4; IfcMaterial
    gained Category. Reading them off a 2X3 file must not blow up.
    """
    file = ifcopenshell.file(schema="IFC2X3")
    wall = file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    layer_set = file.create_entity(
        "IfcMaterialLayerSet",
        MaterialLayers=[
            file.create_entity(
                "IfcMaterialLayer",
                Material=file.create_entity("IfcMaterial", Name="Concrete"),
                LayerThickness=240.0,
            )
        ],
    )
    _associate_material(file, wall, layer_set)

    (material,) = extract_properties(wall)["Materials"]

    assert material["name"] == "Concrete"
    assert material["thickness"]["value"] == 240.0
    assert "category" not in material
