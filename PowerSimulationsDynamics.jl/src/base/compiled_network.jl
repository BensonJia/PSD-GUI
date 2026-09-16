const COMPILED_NETWORK_SCHEMA_VERSION = 1
const COMPILED_NETWORK_FILENAME = "network.bin"

struct CompiledInjector
    name::String
    static_type::String
    dynamic_type::String
    bus_number::Int
    ix_range::Vector{Int}
    ode_range::Vector{Int}
    inner_vars_range::Vector{Int}
    states::Vector{Symbol}
    global_index::Dict{Symbol, Int}
    component_state_mapping::Dict{Int, Vector{Int}}
    input_port_mapping::Dict{Int, Vector{Int}}
end

struct CompiledBranch
    name::String
    branch_type::String
    bus_number_from::Int
    bus_number_to::Int
    ix_range::Vector{Int}
    ode_range::Vector{Int}
    states::Vector{Symbol}
end

"""
    CompiledNetwork

A parameter-free, cross-process description of the executable network structure.
It stores state and port layout, component dispatch types, topology, equation provenance,
and a Jacobian prototype. Numerical parameters and mutable simulation state deliberately
remain in the `PowerSystems.System` that is bound when a simulation is constructed.
"""
struct CompiledNetwork
    schema_version::Int
    fingerprint::String
    model_type::String
    frequency_reference_type::String
    environment::Dict{String, String}
    variable_count::Int
    injection_n_states::Int
    branches_n_states::Int
    inner_vars_count::Int
    ode_start::Int
    dae_vector::Vector{Bool}
    has_delays::Bool
    ybus_colptr::Vector{Int}
    ybus_rowval::Vector{Int}
    injectors::Vector{CompiledInjector}
    dynamic_branches::Vector{CompiledBranch}
    topology::Dict{String, Any}
    equations::Vector{Dict{String, Any}}
    jacobian_prototype::Union{
        Matrix{Float64},
        SparseArrays.SparseMatrixCSC{Float64, Int},
    }
end

function _package_version(mod::Module)
    module_path = pathof(mod)
    isnothing(module_path) && return "development"
    project_file = normpath(joinpath(dirname(module_path), "..", "Project.toml"))
    isfile(project_file) || return "development"
    return string(get(TOML.parsefile(project_file), "version", "development"))
end

function _compiled_environment()
    return Dict(
        "julia" => string(VERSION),
        "kernel" => string(Sys.KERNEL),
        "architecture" => string(Sys.ARCH),
        "PowerSimulationsDynamics" => _package_version(PSID),
        "PowerSystems" => _package_version(PSY),
    )
end

function _validate_compiled_environment(compiled::CompiledNetwork)
    current = _compiled_environment()
    compiled.environment == current && return
    differences = String[]
    for key in sort!(collect(union(keys(compiled.environment), keys(current))))
        cached_value = get(compiled.environment, key, "<missing>")
        current_value = get(current, key, "<missing>")
        cached_value == current_value && continue
        push!(differences, "$key: cached=$cached_value, current=$current_value")
    end
    throw(
        IS.ConflictingInputsError(
            "The compiled network was created in an incompatible environment: " *
            join(differences, "; "),
        ),
    )
end

_safe_available(component) = try
    Bool(PSY.get_available(component))
catch
    true
end

_safe_states(component) = try
    Symbol.(PSY.get_states(component))
catch
    Symbol[]
end

_safe_name(component) = try
    string(PSY.get_name(component))
catch
    string(nameof(typeof(component)))
end

function _safe_bus_number(component)
    return Int(PSY.get_number(PSY.get_bus(component)))
end

_safe_status(component) = try
    Int(PSY.get_status(component))
catch
    1
end

function _network_branches(sys::PSY.System)
    result = Any[]
    seen = Set{Tuple{String, String}}()
    for collection in (
        PSY.get_components(PSY.ACBranch, sys),
        PSY.get_components(PSY.DynamicBranch, sys),
    )
        for branch in collection
            key = (string(typeof(branch)), _safe_name(branch))
            key in seen && continue
            push!(seen, key)
            push!(result, branch)
        end
    end
    return result
end

function _dynamic_children(component)
    children = Any[]
    for field in fieldnames(typeof(component))
        value = getfield(component, field)
        value isa PSY.DeviceParameter || continue
        push!(children, (string(field), value))
    end
    return children
end

function _append_component_structure!(lines::Vector{String}, component, path::String)
    push!(
        lines,
        join(
            (
                "component",
                path,
                string(typeof(component)),
                join(string.(_safe_states(component)), ","),
            ),
            '|',
        ),
    )
    for (field, child) in sort!(_dynamic_children(component); by = first)
        _append_component_structure!(lines, child, "$path/$field")
    end
    return
end

function _source_root()
    return normpath(joinpath(dirname(pathof(PSID)), ".."))
end

function _equation_source_digest()
    source_root = joinpath(_source_root(), "src")
    paths = String[]
    for subdir in ("models", "base")
        root = joinpath(source_root, subdir)
        for (directory, _, files) in walkdir(root)
            for file in files
                endswith(file, ".jl") || continue
                if subdir == "base" &&
                   !(file in (
                       "device_wrapper.jl",
                       "branch_wrapper.jl",
                       "simulation_inputs.jl",
                       "system_model.jl",
                       "jacobian.jl",
                       "mass_matrix.jl",
                       "caches.jl",
                   ))
                    continue
                end
                push!(paths, joinpath(directory, file))
            end
        end
    end
    io = IOBuffer()
    for path in sort!(paths)
        write(io, relpath(path, source_root))
        write(io, UInt8(0))
        write(io, read(path))
        write(io, UInt8(0))
    end
    return bytes2hex(SHA.sha256(take!(io)))
end

function _structure_lines(
    ::Type{T},
    sys::PSY.System,
    frequency_reference::Union{ConstantFrequency, ReferenceBus},
) where {T <: SimulationModel}
    lines = String[
        "schema|$(COMPILED_NETWORK_SCHEMA_VERSION)",
        "model|$(T)",
        "frequency_reference|$(typeof(frequency_reference))",
        "equations|$(_equation_source_digest())",
    ]

    buses = collect(PSY.get_components(PSY.ACBus, sys))
    sort!(buses; by = bus -> PSY.get_number(bus))
    for bus in buses
        push!(
            lines,
            join(
                (
                    "bus",
                    PSY.get_number(bus),
                    _safe_name(bus),
                    string(typeof(bus)),
                    string(PSY.get_bustype(bus)),
                    _safe_available(bus),
                    _safe_status(bus),
                ),
                '|',
            ),
        )
    end

    branches = _network_branches(sys)
    sort!(branches; by = branch -> (string(typeof(branch)), _safe_name(branch)))
    for branch in branches
        arc = PSY.get_arc(branch)
        push!(
            lines,
            join(
                (
                    "branch",
                    _safe_name(branch),
                    string(typeof(branch)),
                    PSY.get_number(arc.from),
                    PSY.get_number(arc.to),
                    _safe_available(branch),
                    _safe_status(branch),
                ),
                '|',
            ),
        )
        if branch isa PSY.DynamicBranch
            _append_component_structure!(lines, branch, "branch/$(_safe_name(branch))")
        end
    end

    injectors = collect(PSY.get_components(PSY.StaticInjection, sys))
    sort!(injectors; by = injector -> (string(typeof(injector)), _safe_name(injector)))
    for injector in injectors
        dynamic = try
            PSY.get_dynamic_injector(injector)
        catch
            nothing
        end
        push!(
            lines,
            join(
                (
                    "injector",
                    _safe_name(injector),
                    string(typeof(injector)),
                    _safe_bus_number(injector),
                    _safe_available(injector),
                    _safe_status(injector),
                    isnothing(dynamic) ? "none" : string(typeof(dynamic)),
                ),
                '|',
            ),
        )
        isnothing(dynamic) ||
            _append_component_structure!(lines, dynamic, "injector/$(_safe_name(injector))")
    end
    return lines
end

"""
    network_fingerprint(ModelType, system; frequency_reference=ReferenceBus())

Return the SHA-256 identity of the network computation graph. Numerical operating-point
values and model parameters are intentionally excluded. Connectivity, availability, concrete
component types, state layouts, frequency-reference strategy, model formulation, and equation
source code are included.
"""
function network_fingerprint(
    ::Type{T},
    sys::PSY.System;
    frequency_reference::Union{ConstantFrequency, ReferenceBus} = ReferenceBus(),
) where {T <: SimulationModel}
    canonical = join(_structure_lines(T, sys, frequency_reference), '\n')
    return bytes2hex(SHA.sha256(codeunits(canonical)))
end

function _equation_role(component)
    component isa PSY.Machine && return ("machine", "models/generator_models/machine_models.jl", ["mdl_machine_ode!"])
    component isa PSY.Shaft && return ("shaft", "models/generator_models/shaft_models.jl", ["mdl_shaft_ode!"])
    component isa PSY.AVR && return ("avr", "models/generator_models/avr_models.jl", ["mdl_avr_ode!"])
    component isa PSY.TurbineGov && return ("governor", "models/generator_models/tg_models.jl", ["mdl_tg_ode!"])
    component isa PSY.PSS && return ("pss", "models/generator_models/pss_models.jl", ["mdl_pss_ode!"])
    component isa PSY.DCSource && return ("dc_source", "models/inverter_models/DCside_models.jl", ["mdl_DCside_ode!"])
    component isa PSY.FrequencyEstimator && return ("frequency_estimator", "models/inverter_models/frequency_estimator_models.jl", ["mdl_freq_estimator_ode!"])
    component isa PSY.OuterControl && return ("outer_control", "models/inverter_models/outer_control_models.jl", ["mdl_outer_ode!"])
    component isa PSY.InnerControl && return ("inner_control", "models/inverter_models/inner_control_models.jl", ["mdl_inner_ode!"])
    component isa PSY.Converter && return ("converter", "models/inverter_models/converter_models.jl", ["mdl_converter_ode!"])
    component isa PSY.Filter && return ("filter", "models/inverter_models/filter_models.jl", ["mdl_filter_ode!"])
    component isa PSY.OutputCurrentLimiter && return ("output_current_limiter", "models/inverter_models/output_current_limiter_models.jl", ["limit_output_current"])
    component isa PSY.DynamicBranch && return ("dynamic_branch", "models/dynline_model.jl", ["mdl_branch_ode!"])
    component isa PSY.DynamicGenerator && return ("dynamic_generator", "models/device.jl", ["device!"])
    component isa PSY.DynamicInverter && return ("dynamic_inverter", "models/device.jl", ["device!"])
    return ("dynamic_injection", "models/device.jl", ["device!"])
end

function _append_equation_nodes!(
    result::Vector{Dict{String, Any}},
    component,
    path::String;
    inherited = nothing,
)
    role, source, entrypoints = _equation_role(component)
    if role == "dynamic_injection" && !isnothing(inherited)
        role, source, entrypoints = inherited
    end
    push!(
        result,
        Dict{String, Any}(
            "path" => path,
            "role" => role,
            "type" => string(typeof(component)),
            "states" => string.(_safe_states(component)),
            "entrypoints" => entrypoints,
            "source" => source,
        ),
    )
    inherited_role = (role, source, entrypoints)
    for (field, child) in sort!(_dynamic_children(component); by = first)
        _append_equation_nodes!(result, child, "$path/$field"; inherited = inherited_role)
    end
    return
end

function _topology_export(sys::PSY.System)
    buses = Dict{String, Any}[]
    for bus in sort!(collect(PSY.get_components(PSY.ACBus, sys)); by = PSY.get_number)
        push!(
            buses,
            Dict(
                "number" => Int(PSY.get_number(bus)),
                "name" => _safe_name(bus),
                "type" => string(typeof(bus)),
                "bus_type" => string(PSY.get_bustype(bus)),
                "available" => _safe_available(bus),
                "status" => _safe_status(bus),
            ),
        )
    end

    branches = Dict{String, Any}[]
    for branch in sort!(_network_branches(sys); by = _safe_name)
        arc = PSY.get_arc(branch)
        push!(
            branches,
            Dict(
                "name" => _safe_name(branch),
                "type" => string(typeof(branch)),
                "from" => Int(PSY.get_number(arc.from)),
                "to" => Int(PSY.get_number(arc.to)),
                "available" => _safe_available(branch),
                "status" => _safe_status(branch),
                "dynamic" => branch isa PSY.DynamicBranch,
            ),
        )
    end

    injections = Dict{String, Any}[]
    for injector in sort!(
        collect(PSY.get_components(PSY.StaticInjection, sys));
        by = _safe_name,
    )
        dynamic = try
            PSY.get_dynamic_injector(injector)
        catch
            nothing
        end
        push!(
            injections,
            Dict(
                "name" => _safe_name(injector),
                "type" => string(typeof(injector)),
                "bus" => _safe_bus_number(injector),
                "available" => _safe_available(injector),
                "status" => _safe_status(injector),
                "dynamic_type" => isnothing(dynamic) ? "" : string(typeof(dynamic)),
            ),
        )
    end
    return Dict{String, Any}(
        "buses" => buses,
        "branches" => branches,
        "injections" => injections,
    )
end

function _equation_export(inputs::SimulationInputs, sys::PSY.System)
    result = Dict{String, Any}[
        Dict(
            "path" => "network",
            "role" => "network",
            "type" => "current_injection_balance",
            "states" => String[],
            "entrypoints" => ["network_model"],
            "source" => "models/network_model.jl",
        ),
        Dict(
            "path" => "system",
            "role" => "system_assembly",
            "type" => "system_model",
            "states" => String[],
            "entrypoints" => ["system_mass_matrix!", "system_residual!"],
            "source" => "models/system.jl",
        ),
    ]
    for wrapper in get_dynamic_injectors(inputs)
        device = get_device(wrapper)
        _append_equation_nodes!(
            result,
            device,
            "injections/$(PSY.get_name(wrapper))",
        )
    end
    for wrapper in get_dynamic_branches(inputs)
        branch = get_branch(wrapper)
        _append_equation_nodes!(
            result,
            branch,
            "branches/$(PSY.get_name(wrapper))",
        )
    end
    for injector in PSY.get_components(PSY.StaticInjection, sys)
        dynamic = try
            PSY.get_dynamic_injector(injector)
        catch
            nothing
        end
        (!isnothing(dynamic) || !_safe_available(injector)) && continue
        role, source, entrypoints = if injector isa PSY.Source
            ("source", "models/source_models.jl", ["mdl_source!"])
        elseif injector isa PSY.ElectricLoad
            ("load", "models/load_models.jl", ["mdl_zip_load!", "device!"])
        else
            ("static_injection", "models/device.jl", ["device!"])
        end
        push!(
            result,
            Dict{String, Any}(
                "path" => "injections/$(_safe_name(injector))",
                "role" => role,
                "type" => string(typeof(injector)),
                "states" => String[],
                "entrypoints" => entrypoints,
                "source" => source,
            ),
        )
    end
    return result
end

function _problem_jacobian(sim)
    isnothing(sim.problem) && error("The simulation must be built before it can be compiled")
    jacobian = getproperty(sim.problem.f, :jac)
    jacobian isa JacobianFunctionWrapper ||
        error("The built problem does not expose a PowerSimulationsDynamics Jacobian")
    return jacobian
end

"""
    compile_network(simulation)

Capture the parameter-free computation graph of a built simulation. The returned object can
be serialized with [`save_compiled_network`](@ref), loaded in another process, and rebound to
a structurally identical `PowerSystems.System` with different numerical parameters.
"""
function compile_network(sim)
    inputs = get_simulation_inputs(sim)
    isnothing(inputs) && error("The simulation must be built before it can be compiled")
    model_type = typeof(sim).parameters[1]
    fingerprint = network_fingerprint(
        model_type,
        get_system(sim);
        frequency_reference = sim.frequency_reference,
    )
    injectors = CompiledInjector[]
    current_injectors = collect(get_injectors_with_dynamics(get_system(sim)))
    for wrapper in get_dynamic_injectors(inputs)
        device = get_device(wrapper)
        static = _find_static_injector(current_injectors, device)
        push!(
            injectors,
            CompiledInjector(
                PSY.get_name(static),
                string(typeof(static)),
                string(typeof(device)),
                Int(PSY.get_number(PSY.get_bus(static))),
                copy(get_ix_range(wrapper)),
                copy(get_ode_ouput_range(wrapper)),
                copy(get_inner_vars_index(wrapper)),
                copy(_safe_states(device)),
                Dict{Symbol, Int}(get_global_index(wrapper)),
                Dict{Int, Vector{Int}}(get_component_state_mapping(wrapper)),
                Dict{Int, Vector{Int}}(get_input_port_mapping(wrapper)),
            ),
        )
    end
    branches = CompiledBranch[]
    for wrapper in get_dynamic_branches(inputs)
        branch = get_branch(wrapper)
        arc = PSY.get_arc(branch)
        push!(
            branches,
            CompiledBranch(
                PSY.get_name(wrapper),
                string(typeof(branch)),
                Int(PSY.get_number(arc.from)),
                Int(PSY.get_number(arc.to)),
                copy(get_ix_range(wrapper)),
                copy(get_ode_ouput_range(wrapper)),
                copy(_safe_states(branch)),
            ),
        )
    end
    jacobian = _problem_jacobian(sim)
    return CompiledNetwork(
        COMPILED_NETWORK_SCHEMA_VERSION,
        fingerprint,
        string(model_type),
        string(typeof(sim.frequency_reference)),
        _compiled_environment(),
        get_variable_count(inputs),
        get_injection_n_states(inputs),
        get_branches_n_states(inputs),
        get_inner_vars_count(inputs),
        first(get_ode_ouput_range(inputs)),
        collect(get_DAE_vector(inputs)),
        !isempty(inputs.delays),
        copy(get_ybus(inputs).colptr),
        copy(get_ybus(inputs).rowval),
        injectors,
        branches,
        _topology_export(get_system(sim)),
        _equation_export(inputs, get_system(sim)),
        copy(jacobian.Jv),
    )
end

function _compiled_file(path::AbstractString)
    return isdir(path) ? joinpath(path, "compiled", COMPILED_NETWORK_FILENAME) : path
end

"""
    load_compiled_network(path)

Load a compiled computation graph from an exported network directory or directly from its
`compiled/network.bin` file. Environment and network compatibility are checked when it is
bound to a simulation.
"""
function load_compiled_network(path::AbstractString)
    file = _compiled_file(path)
    isfile(file) || throw(ArgumentError("Compiled network file not found: $file"))
    compiled = open(Serialization.deserialize, file)
    compiled isa CompiledNetwork ||
        throw(ArgumentError("File does not contain a CompiledNetwork: $file"))
    compiled.schema_version == COMPILED_NETWORK_SCHEMA_VERSION ||
        throw(
            ArgumentError(
                "Unsupported compiled network schema $(compiled.schema_version); expected $(COMPILED_NETWORK_SCHEMA_VERSION)",
            ),
        )
    return compiled
end

function _write_toml(path::AbstractString, contents::Dict{String, Any})
    mkpath(dirname(path))
    temporary = tempname(dirname(path))
    open(temporary, "w") do io
        TOML.print(io, contents)
    end
    mv(temporary, path; force = true)
    return
end

function _mapping_export(mapping::Dict{Int, Vector{Int}})
    return [
        Dict{String, Any}("component_index" => key, "indices" => value) for
        (key, value) in sort!(collect(mapping); by = first)
    ]
end

function _graph_export(compiled::CompiledNetwork)
    injectors = Dict{String, Any}[]
    for node in compiled.injectors
        push!(
            injectors,
            Dict{String, Any}(
                "name" => node.name,
                "static_type" => node.static_type,
                "dynamic_type" => node.dynamic_type,
                "bus" => node.bus_number,
                "states" => string.(node.states),
                "state_indices" => node.ix_range,
                "ode_indices" => node.ode_range,
                "inner_variable_indices" => node.inner_vars_range,
                "global_state_indices" => [
                    Dict{String, Any}("state" => string(key), "index" => value) for
                    (key, value) in sort!(collect(node.global_index); by = last)
                ],
                "component_state_mapping" =>
                    _mapping_export(node.component_state_mapping),
                "input_port_mapping" => _mapping_export(node.input_port_mapping),
            ),
        )
    end
    branches = Dict{String, Any}[]
    for node in compiled.dynamic_branches
        push!(
            branches,
            Dict{String, Any}(
                "name" => node.name,
                "type" => node.branch_type,
                "from" => node.bus_number_from,
                "to" => node.bus_number_to,
                "states" => string.(node.states),
                "state_indices" => node.ix_range,
                "ode_indices" => node.ode_range,
            ),
        )
    end
    return Dict{String, Any}(
        "variable_count" => compiled.variable_count,
        "injection_state_count" => compiled.injection_n_states,
        "branch_state_count" => compiled.branches_n_states,
        "inner_variable_count" => compiled.inner_vars_count,
        "ode_start" => compiled.ode_start,
        "dae_vector" => compiled.dae_vector,
        "has_delays" => compiled.has_delays,
        "ybus_colptr" => compiled.ybus_colptr,
        "ybus_rowval" => compiled.ybus_rowval,
        "injectors" => injectors,
        "dynamic_branches" => branches,
    )
end

function _copy_equation_sources(compiled::CompiledNetwork, directory::AbstractString)
    source_root = joinpath(_source_root(), "src")
    sources = sort!(unique(String[node["source"] for node in compiled.equations]))
    for relative in sources
        source = joinpath(source_root, relative)
        isfile(source) || continue
        destination = joinpath(directory, "equations", "sources", relative)
        mkpath(dirname(destination))
        cp(source, destination; force = true)
    end
    return sources
end

function _write_equation_readme(
    compiled::CompiledNetwork,
    directory::AbstractString,
    sources::Vector{String},
)
    path = joinpath(directory, "equations", "README.md")
    mkpath(dirname(path))
    open(path, "w") do io
        println(io, "# Network equation hierarchy")
        println(io)
        println(io, "Graph fingerprint: `$(compiled.fingerprint)`")
        println(io)
        println(io, "The TOML index is machine-readable. The copied Julia files contain the human-readable equation-method families used by the selected component types.")
        println(io)
        println(io, "## Components")
        println(io)
        for node in compiled.equations
            node_path = node["path"]
            node_type = node["type"]
            entrypoints = join(node["entrypoints"], ", ")
            source = node["source"]
            println(
                io,
                "- `$node_path`: `$node_type` — $entrypoints (`$source`)",
            )
        end
        println(io)
        println(io, "## Source files")
        println(io)
        for source in sources
            println(io, "- `sources/$source`")
        end
    end
    return
end

function _manifest(compiled::CompiledNetwork, include_compiled::Bool)
    return Dict{String, Any}(
        "schema_version" => compiled.schema_version,
        "fingerprint" => compiled.fingerprint,
        "model_type" => compiled.model_type,
        "frequency_reference_type" => compiled.frequency_reference_type,
        "created_at_utc" => string(Dates.now(Dates.UTC)),
        "parameter_policy" => "runtime values excluded; structure-changing values forbidden",
        "compiled_cache" => include_compiled ? "compiled/network.bin" : "",
        "environment" => compiled.environment,
    )
end

"""
    save_compiled_network(compiled_or_simulation, directory; include_compiled=true)

Export a network project directory containing `manifest.toml`, `topology.toml`, a hierarchical
machine-readable equation index, human-readable equation sources, and optionally the binary
compiled computation graph.
"""
function save_compiled_network(
    compiled::CompiledNetwork,
    directory::AbstractString;
    include_compiled::Bool = true,
)
    mkpath(directory)
    _write_toml(joinpath(directory, "manifest.toml"), _manifest(compiled, include_compiled))
    _write_toml(joinpath(directory, "topology.toml"), compiled.topology)
    _write_toml(joinpath(directory, "graph.toml"), _graph_export(compiled))
    _write_toml(
        joinpath(directory, "equations", "index.toml"),
        Dict{String, Any}("components" => compiled.equations),
    )
    sources = _copy_equation_sources(compiled, directory)
    _write_equation_readme(compiled, directory, sources)
    if include_compiled
        target = joinpath(directory, "compiled", COMPILED_NETWORK_FILENAME)
        mkpath(dirname(target))
        temporary = tempname(dirname(target))
        open(temporary, "w") do io
            Serialization.serialize(io, compiled)
        end
        mv(temporary, target; force = true)
    end
    return abspath(directory)
end

function save_compiled_network(sim, directory::AbstractString; include_compiled::Bool = true)
    return save_compiled_network(
        compile_network(sim),
        directory;
        include_compiled = include_compiled,
    )
end

"""
    export_network(simulation, directory; include_compiled=true, include_system=true)

Export a complete, inspectable network project. In addition to the parameter-free graph,
topology, and equation hierarchy, the simulation overload writes the current parameterized
`PowerSystems.System` as `system.json` by default.
"""
function export_network(
    compiled::CompiledNetwork,
    directory::AbstractString;
    include_compiled::Bool = true,
)
    return save_compiled_network(
        compiled,
        directory;
        include_compiled = include_compiled,
    )
end

function export_network(
    sim,
    directory::AbstractString;
    include_compiled::Bool = true,
    include_system::Bool = true,
)
    result = save_compiled_network(
        sim,
        directory;
        include_compiled = include_compiled,
    )
    include_system && PSY.to_json(get_system(sim), joinpath(directory, "system.json"))
    return result
end

function _find_named_component(components, name::String, description::String)
    for component in components
        PSY.get_name(component) == name && return component
    end
    throw(
        IS.ConflictingInputsError(
            "The compiled graph expects $description '$name', but it is absent from the runtime system",
        ),
    )
end

function _find_static_injector(injectors, dynamic)
    for injector in injectors
        PSY.get_dynamic_injector(injector) === dynamic && return injector
    end
    dynamic_name = PSY.get_name(dynamic)
    return _find_named_component(injectors, dynamic_name, "dynamic injector")
end

function _as_immutable_dict(values::Dict{K, V}) where {K, V}
    isempty(values) && return Base.ImmutableDict{K, V}()
    return Base.ImmutableDict(values...)
end

function _dynamic_reference_values(static, dynamic)
    if static isa PSY.Source
        return (V = 0.0, omega = 0.0, P = 0.0, Q = 0.0)
    end
    reactive_power =
        static isa PSY.StandardLoad ? get_total_q(static) : PSY.get_reactive_power(static)
    return (
        V = PSY.get_V_ref(dynamic),
        omega = PSY.get_ω_ref(dynamic),
        P = PSY.get_P_ref(dynamic),
        Q = reactive_power,
    )
end

function _bind_dynamic_wrapper(
    node::CompiledInjector,
    static,
    dynamic,
    bus_ix::Int,
    sys_base_power::Float64,
    sys_base_frequency::Float64,
)
    references = _dynamic_reference_values(static, dynamic)
    return DynamicWrapper(
        dynamic,
        sys_base_power,
        sys_base_frequency,
        typeof(static),
        BUS_MAP[PSY.get_bustype(PSY.get_bus(static))],
        Base.Ref(1.0),
        Base.Ref(references.V),
        Base.Ref(references.omega),
        Base.Ref(references.P),
        Base.Ref(references.Q),
        node.inner_vars_range,
        node.ix_range,
        node.ode_range,
        bus_ix,
        _as_immutable_dict(node.global_index),
        _as_immutable_dict(node.component_state_mapping),
        _as_immutable_dict(node.input_port_mapping),
        Dict{String, Any}(),
    )
end

function _validate_compiled_structure(
    ::Type{T},
    sys::PSY.System,
    frequency_reference::Union{ConstantFrequency, ReferenceBus},
    compiled::CompiledNetwork,
) where {T <: SimulationModel}
    _validate_compiled_environment(compiled)
    fingerprint = network_fingerprint(T, sys; frequency_reference = frequency_reference)
    fingerprint == compiled.fingerprint ||
        throw(
            IS.ConflictingInputsError(
                "The runtime system structure does not match the compiled network. " *
                "cached=$(compiled.fingerprint), current=$fingerprint",
            ),
        )
    return
end

function _bind_compiled_inputs(
    ::Type{T},
    sys::PSY.System,
    frequency_reference::Union{ConstantFrequency, ReferenceBus},
    compiled::CompiledNetwork,
) where {T <: SimulationModel}
    _validate_compiled_structure(T, sys, frequency_reference, compiled)
    n_buses = get_n_buses(sys)
    ybus, lookup = _get_ybus(sys)
    (ybus.colptr == compiled.ybus_colptr && ybus.rowval == compiled.ybus_rowval) ||
        throw(IS.ConflictingInputsError("Ybus sparsity structure changed"))
    sys_base_power = PSY.get_base_power(sys)
    sys_base_frequency = PSY.get_frequency(sys)

    current_injectors = collect(get_injectors_with_dynamics(sys))
    wrapped_injectors = Vector(undef, length(compiled.injectors))
    for (index, node) in enumerate(compiled.injectors)
        static = _find_named_component(current_injectors, node.name, "dynamic injector")
        dynamic = PSY.get_dynamic_injector(static)
        configure_dynamic_device!(dynamic, lookup)
        string(typeof(static)) == node.static_type ||
            throw(IS.ConflictingInputsError("Static type changed for $(node.name)"))
        string(typeof(dynamic)) == node.dynamic_type ||
            throw(IS.ConflictingInputsError("Dynamic type changed for $(node.name)"))
        _safe_states(dynamic) == node.states ||
            throw(IS.ConflictingInputsError("State layout changed for $(node.name)"))
        wrapped_injectors[index] = _bind_dynamic_wrapper(
            node,
            static,
            dynamic,
            lookup[node.bus_number],
            sys_base_power,
            sys_base_frequency,
        )
    end

    current_branches = collect(get_dynamic_branches(sys))
    wrapped_branches = Vector{BranchWrapper}(undef, length(compiled.dynamic_branches))
    for (index, node) in enumerate(compiled.dynamic_branches)
        branch = _find_named_component(current_branches, node.name, "dynamic branch")
        string(typeof(branch)) == node.branch_type ||
            throw(IS.ConflictingInputsError("Dynamic branch type changed for $(node.name)"))
        _safe_states(branch) == node.states ||
            throw(IS.ConflictingInputsError("Dynamic branch state layout changed for $(node.name)"))
        wrapped_branches[index] = BranchWrapper(
            branch,
            lookup[node.bus_number_from],
            lookup[node.bus_number_to],
            node.ix_range,
            node.ode_range,
            sys_base_power,
            sys_base_frequency,
        )
    end

    mass_matrix = _make_mass_matrix(wrapped_injectors, compiled.variable_count, n_buses)
    dae_vector = _make_DAE_vector(mass_matrix, compiled.variable_count, n_buses)
    total_shunts = _make_total_shunts(wrapped_branches, n_buses)
    _adjust_states!(
        dae_vector,
        mass_matrix,
        total_shunts,
        n_buses,
        sys_base_frequency,
    )
    collect(dae_vector) == compiled.dae_vector ||
        throw(IS.ConflictingInputsError("Differential/algebraic state layout changed"))
    static_injectors = _wrap_static_injectors(sys, lookup)
    static_loads = _wrap_loads(sys, lookup)
    global_vars =
        _make_global_variable_index(wrapped_injectors, static_injectors, typeof(frequency_reference))
    delays = get_system_delays(sys)
    isempty(delays) == !compiled.has_delays ||
        throw(IS.ConflictingInputsError("Delay structure changed"))
    return SimulationInputs(
        wrapped_injectors,
        static_injectors,
        static_loads,
        wrapped_branches,
        compiled.injection_n_states,
        compiled.branches_n_states,
        compiled.variable_count,
        compiled.inner_vars_count,
        n_buses,
        compiled.ode_start:compiled.variable_count,
        ybus,
        !isempty(wrapped_branches),
        total_shunts,
        lookup,
        dae_vector,
        mass_matrix,
        global_vars,
        MAPPING_DICT(),
        Dict{String, Dict}(),
        delays,
    )
end

function _resolve_compiled_network(value)
    isnothing(value) && return nothing
    value isa CompiledNetwork && return value
    value isa AbstractString && return load_compiled_network(value)
    throw(ArgumentError("compiled_network must be a CompiledNetwork, a path, or nothing"))
end

function _network_cache_directory(root::AbstractString, fingerprint::AbstractString)
    return joinpath(abspath(root), fingerprint)
end
