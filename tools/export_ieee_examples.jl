using Downloads
using Logging
using PowerSystems
using PowerSimulationsDynamics

const PSY = PowerSystems
const PSID = PowerSimulationsDynamics
const PSB = Base.require(
    Base.PkgId(
        Base.UUID("f00506e0-b84f-492a-93c2-c0a9afc4364e"),
        "PowerSystemCaseBuilder",
    ),
)

const EXAMPLES = normpath(joinpath(@__DIR__, "..", "Examples"))

function add_classical_generators!(system)
    for generator in PSY.get_components(PSY.Generator, system)
        dynamic_generator = PSY.DynamicGenerator(;
            name = PSY.get_name(generator),
            ω_ref = 1.0,
            machine = PSY.BaseMachine(; R = 0.0, Xd_p = 0.3, eq_p = 1.0),
            shaft = PSY.SingleMass(; H = 5.0, D = 1.0),
            avr = PSY.AVRFixed(; Vf = 1.0),
            prime_mover = PSY.TGFixed(; efficiency = 1.0),
            pss = PSY.PSSFixed(; V_pss = 0.0),
        )
        PSY.add_component!(system, dynamic_generator, generator)
    end
    for load in PSY.get_components(PSY.StandardLoad, system)
        PSY.transform_load_to_constant_impedance(load)
    end
    return system
end

function ieee39_system()
    source = Downloads.download(
        "https://raw.githubusercontent.com/MATPOWER/matpower/master/data/case39.m",
    )
    system = PSY.System(source; runchecks = false)
    return add_classical_generators!(system)
end

function export_example(system, node_count)
    workdir = mktempdir()
    simulation = PSID.Simulation(
        PSID.MassMatrixModel,
        system,
        workdir,
        (0.0, 0.1);
        initialize_simulation = false,
        console_level = Logging.Error,
    )
    simulation.status == PSID.BUILT || error("IEEE $node_count build failed")
    destination = joinpath(EXAMPLES, "IEEE_$node_count")
    PSID.export_network(simulation, destination)
    compiled = PSID.load_compiled_network(destination)
    bus_count = length(collect(PSY.get_components(PSY.ACBus, system)))
    bus_count == node_count || error("IEEE $node_count has $bus_count buses")
    println("IEEE $node_count: $(compiled.variable_count) variables -> $destination")
end

mkpath(EXAMPLES)
export_example(PSB.build_system(PSB.PSIDTestSystems, "psid_test_ieee_9bus"), 9)
export_example(PSB.build_system(PSB.PSIDSystems, "14 Bus Base Case"), 14)
export_example(ieee39_system(), 39)
