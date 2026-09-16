using TOML
using JSON3
using SHA
import Pkg
using PowerSimulationsDynamics
using PowerSystems
using Sundials
using OrdinaryDiffEq

const OUTPUT_LOCK = ReentrantLock()
const MEMORY_NETWORKS = Dict{String, Any}()
const JOB_LOCK = ReentrantLock()
const JOB_BUSY = Ref(false)
const CANCEL_REQUESTED = Threads.Atomic{Bool}(false)
const SHUTDOWN_REQUESTED = Threads.Atomic{Bool}(false)

# A data-driven DynamicInjection used by the GUI model editor.  Expressions are parsed
# once and interpreted through the small whitelist below; project files cannot execute
# arbitrary Julia code.
mutable struct GUIEquationModel <: PowerSystems.DynamicInjection
    name::String
    base_power::Float64
    states::Vector{Symbol}
    masses::Vector{Float64}
    initial_values::Vector{Float64}
    initialization::Symbol
    parameters::Dict{Symbol, Float64}
    equations::Vector{Any}
    current_r_expression::Any
    current_i_expression::Any
    P_ref::Float64
    Q_ref::Float64
    V_ref::Float64
    omega_ref::Float64
    ext::Dict{String, Any}
    internal::PowerSystems.InfrastructureSystemsInternal
end

mutable struct GUITopologyModel <: PowerSystems.DynamicInjection
    name::String
    base_power::Float64
    states::Vector{Symbol}
    masses::Vector{Float64}
    initial_values::Vector{Float64}
    parameters::Dict{Symbol, Float64}
    equations::Vector{Any}
    port_ids::Vector{Symbol}
    bus_numbers::Vector{Int}
    bus_indices::Vector{Int}
    current_r_expressions::Vector{Any}
    current_i_expressions::Vector{Any}
    ext::Dict{String, Any}
    internal::PowerSystems.InfrastructureSystemsInternal
end

PowerSystems.get_name(value::GUIEquationModel) = value.name
PowerSystems.get_base_power(value::GUIEquationModel) = value.base_power
PowerSystems.get_states(value::GUIEquationModel) = value.states
PowerSystems.get_n_states(value::GUIEquationModel) = length(value.states)
PowerSystems.get_ext(value::GUIEquationModel) = value.ext
PowerSystems.get_internal(value::GUIEquationModel) = value.internal
PowerSystems.get_V_ref(value::GUIEquationModel) = value.V_ref
PowerSystems.get_ω_ref(value::GUIEquationModel) = value.omega_ref
PowerSystems.get_P_ref(value::GUIEquationModel) = value.P_ref
PowerSystems.get_Q_ref(value::GUIEquationModel) = value.Q_ref
PowerSystems.set_base_power!(value::GUIEquationModel, base_power) = value.base_power = base_power

PowerSystems.get_name(value::GUITopologyModel) = value.name
PowerSystems.get_base_power(value::GUITopologyModel) = value.base_power
PowerSystems.get_states(value::GUITopologyModel) = value.states
PowerSystems.get_n_states(value::GUITopologyModel) = length(value.states)
PowerSystems.get_ext(value::GUITopologyModel) = value.ext
PowerSystems.get_internal(value::GUITopologyModel) = value.internal
PowerSystems.get_V_ref(::GUITopologyModel) = 1.0
PowerSystems.get_ω_ref(::GUITopologyModel) = 1.0
PowerSystems.get_P_ref(::GUITopologyModel) = 0.0
PowerSystems.get_Q_ref(::GUITopologyModel) = 0.0
PowerSystems.get_R_th(::GUITopologyModel) = 0.0
PowerSystems.get_X_th(::GUITopologyModel) = 0.0
PowerSystems.get_bus_numbers(value::GUITopologyModel) = value.bus_numbers
PowerSystems.get_available(::GUITopologyModel) = true
PowerSystems.set_base_power!(value::GUITopologyModel, base_power) = value.base_power = base_power

PowerSimulationsDynamics.is_valid(::GUIEquationModel) = nothing
PowerSimulationsDynamics.get_inner_vars_count(::GUIEquationModel) = 0
PowerSimulationsDynamics.get_delays(::GUIEquationModel) = nothing
PowerSimulationsDynamics.is_valid(::GUITopologyModel) = nothing
PowerSimulationsDynamics.get_inner_vars_count(::GUITopologyModel) = 0
PowerSimulationsDynamics.get_delays(::GUITopologyModel) = nothing

function PowerSimulationsDynamics.configure_dynamic_device!(
    model::GUITopologyModel,
    lookup,
)
    model.bus_indices = [lookup[number] for number in model.bus_numbers]
    return
end

const GUI_FUNCTIONS = Dict{Symbol, Any}(
    :sin => sin,
    :cos => cos,
    :tan => tan,
    :asin => asin,
    :acos => acos,
    :atan => atan,
    :exp => exp,
    :log => log,
    :sqrt => sqrt,
    :abs => abs,
    :min => min,
    :max => max,
    :clamp => clamp,
)

const GUI_INPUTS = Set((
    :t,
    :v_r,
    :v_i,
    :omega_sys,
    :P_ref,
    :Q_ref,
    :V_ref,
    :omega_ref,
    :system_base_power,
    :device_base_power,
    :pi,
    :e,
))

function validate_gui_expression(value, symbols)
    value isa Number && return
    if value isa Symbol
        value in symbols || error("自定义模型包含未定义符号：$value")
        return
    end
    value isa Expr && value.head === :call ||
        error("自定义模型仅支持数学函数和算术运算")
    operator = value.args[1]
    operator in (:+, :-, :*, :/, :^) || haskey(GUI_FUNCTIONS, operator) ||
        error("自定义模型不允许调用函数：$operator")
    for argument in value.args[2:end]
        validate_gui_expression(argument, symbols)
    end
    return
end

function gui_expression(value, environment)
    value isa Number && return value
    if value isa Symbol
        value === :pi && return π
        value === :e && return ℯ
        haskey(environment, value) || error("自定义模型包含未定义符号：$value")
        return environment[value]
    end
    value isa Expr || error("自定义模型包含不支持的表达式节点：$(typeof(value))")
    value.head === :call || error("自定义模型仅支持数学函数和算术运算")
    operator = value.args[1]
    arguments = [gui_expression(argument, environment) for argument in value.args[2:end]]
    operator === :+ && return +(arguments...)
    operator === :- && return -(arguments...)
    operator === :* && return *(arguments...)
    operator === :/ && return /(arguments...)
    operator === :^ && return ^(arguments...)
    haskey(GUI_FUNCTIONS, operator) || error("自定义模型不允许调用函数：$operator")
    return GUI_FUNCTIONS[operator](arguments...)
end

function flatten_gui_properties(value)
    value isa Expr || return value
    if value.head === :. && length(value.args) == 2 && value.args[1] isa Symbol
        property = value.args[2]
        property = property isa QuoteNode ? property.value : property
        property isa Symbol || error("自定义拓扑模型包含无效成员访问")
        return Symbol(value.args[1], "__", property)
    end
    return Expr(value.head, (flatten_gui_properties(argument) for argument in value.args)...)
end

function topology_assignments(body, states, ports)
    parsed = Meta.parse("begin\n$body\nend")
    statements = parsed.head === :block ? parsed.args : Any[parsed]
    equations = Dict{Symbol, Any}()
    outputs = Dict{Symbol, Any}()
    allowed_states = Set(states)
    allowed_outputs = Set(
        Symbol(port, suffix) for port in ports for suffix in ("_i_r", "_i_i")
    )
    for statement in statements
        statement isa LineNumberNode && continue
        statement isa Expr && statement.head === :(=) ||
            error("自定义拓扑模型仅支持 dx.* 和 y.* 赋值语句")
        left, right = statement.args
        left isa Expr && left.head === :. || error("自定义拓扑模型赋值目标无效")
        root = left.args[1]
        name = left.args[2] isa QuoteNode ? left.args[2].value : left.args[2]
        if root === :dx
            name in allowed_states || error("未知状态导数：$name")
            equations[name] = flatten_gui_properties(right)
        elseif root === :y
            name in allowed_outputs || error("未知端口输出：$name")
            outputs[name] = flatten_gui_properties(right)
        else
            error("自定义拓扑模型只能写入 dx 或 y")
        end
    end
    all(haskey(equations, state) for state in states) || error("自定义拓扑模型缺少状态方程")
    all(haskey(outputs, output) for output in allowed_outputs) || error("自定义拓扑模型缺少端口电流输出")
    return [equations[state] for state in states], outputs
end

function gui_environment(states, voltage_r, voltage_i, sys_ω, wrapper, t)
    model = PowerSimulationsDynamics.get_device(wrapper)
    T = eltype(states)
    environment = Dict{Symbol, Any}(
        :t => t,
        :v_r => voltage_r,
        :v_i => voltage_i,
        :omega_sys => sys_ω,
        :P_ref => PowerSimulationsDynamics.get_P_ref(wrapper),
        :Q_ref => PowerSimulationsDynamics.get_Q_ref(wrapper),
        :V_ref => PowerSimulationsDynamics.get_V_ref(wrapper),
        :omega_ref => PowerSimulationsDynamics.get_ω_ref(wrapper),
        :system_base_power => PowerSimulationsDynamics.get_system_base_power(wrapper),
        :device_base_power => model.base_power,
    )
    for (name, value) in model.parameters
        environment[name] = convert(T, value)
    end
    for (index, name) in enumerate(model.states)
        environment[name] = states[index]
    end
    return environment
end

function PowerSimulationsDynamics.device_mass_matrix_entries!(
    mass_matrix::AbstractArray,
    wrapper::PowerSimulationsDynamics.DynamicWrapper{GUIEquationModel},
)
    model = PowerSimulationsDynamics.get_device(wrapper)
    indices = PowerSimulationsDynamics.get_global_index(wrapper)
    for (state, mass) in zip(model.states, model.masses)
        mass_matrix[indices[state], indices[state]] = mass
    end
    return
end

function PowerSimulationsDynamics.device_mass_matrix_entries!(
    mass_matrix::AbstractArray,
    wrapper::PowerSimulationsDynamics.DynamicWrapper{GUITopologyModel},
)
    model = PowerSimulationsDynamics.get_device(wrapper)
    indices = PowerSimulationsDynamics.get_global_index(wrapper)
    for (state, mass) in zip(model.states, model.masses)
        mass_matrix[indices[state], indices[state]] = mass
    end
    return
end

function PowerSimulationsDynamics.network_device!(
    device_states::AbstractArray{T},
    output_ode::AbstractArray{T},
    voltage_r::AbstractArray{T},
    voltage_i::AbstractArray{T},
    current_r::AbstractArray{T},
    current_i::AbstractArray{T},
    global_vars::AbstractArray{T},
    ::AbstractArray{T},
    wrapper::PowerSimulationsDynamics.DynamicWrapper{GUITopologyModel},
    h,
    t,
) where {T <: PowerSimulationsDynamics.ACCEPTED_REAL_TYPES}
    if PowerSimulationsDynamics.get_connection_status(wrapper) < 1.0
        output_ode .= zero(T)
        return
    end
    model = PowerSimulationsDynamics.get_device(wrapper)
    environment = Dict{Symbol, Any}(:t => t, :pi => π, :e => ℯ)
    for (name, value) in model.parameters
        environment[Symbol(:p, "__", name)] = convert(T, value)
    end
    for (index, name) in enumerate(model.states)
        environment[Symbol(:x, "__", name)] = device_states[index]
    end
    for (port, bus_ix) in zip(model.port_ids, model.bus_indices)
        environment[Symbol(:u, "__", port, :_v_r)] = voltage_r[bus_ix]
        environment[Symbol(:u, "__", port, :_v_i)] = voltage_i[bus_ix]
    end
    for index in eachindex(model.equations)
        output_ode[index] = gui_expression(model.equations[index], environment)
    end
    scale = model.base_power / PowerSimulationsDynamics.get_system_base_power(wrapper)
    for (index, bus_ix) in enumerate(model.bus_indices)
        current_r[bus_ix] += scale * gui_expression(model.current_r_expressions[index], environment)
        current_i[bus_ix] += scale * gui_expression(model.current_i_expressions[index], environment)
    end
    return
end

function PowerSimulationsDynamics.initialize_dynamic_device!(
    wrapper::PowerSimulationsDynamics.DynamicWrapper{GUITopologyModel},
    ::PowerSystems.StaticInjection,
    ::AbstractVector,
)
    return copy(PowerSimulationsDynamics.get_device(wrapper).initial_values)
end

function PowerSimulationsDynamics.device!(
    device_states::AbstractArray{T},
    output_ode::AbstractArray{T},
    voltage_r::T,
    voltage_i::T,
    current_r::AbstractArray{T},
    current_i::AbstractArray{T},
    global_vars::AbstractArray{T},
    ::AbstractArray{T},
    wrapper::PowerSimulationsDynamics.DynamicWrapper{GUIEquationModel},
    h,
    t,
) where {T <: PowerSimulationsDynamics.ACCEPTED_REAL_TYPES}
    if PowerSimulationsDynamics.get_connection_status(wrapper) < 1.0
        output_ode .= zero(T)
        return
    end
    model = PowerSimulationsDynamics.get_device(wrapper)
    environment = gui_environment(
        device_states,
        voltage_r,
        voltage_i,
        global_vars[PowerSimulationsDynamics.GLOBAL_VAR_SYS_FREQ_INDEX],
        wrapper,
        t,
    )
    for index in eachindex(model.equations)
        output_ode[index] = gui_expression(model.equations[index], environment)
    end
    scale = model.base_power / PowerSimulationsDynamics.get_system_base_power(wrapper)
    current_r[1] += scale * gui_expression(model.current_r_expression, environment)
    current_i[1] += scale * gui_expression(model.current_i_expression, environment)
    return
end

function PowerSimulationsDynamics.initialize_dynamic_device!(
    wrapper::PowerSimulationsDynamics.DynamicWrapper{GUIEquationModel},
    static::PowerSystems.StaticInjection,
    ::AbstractVector,
)
    model = PowerSimulationsDynamics.get_device(wrapper)
    values = copy(model.initial_values)
    model.initialization === :fixed && return values
    bus = PowerSystems.get_bus(static)
    magnitude = PowerSystems.get_magnitude(bus)
    angle = PowerSystems.get_angle(bus)
    voltage_r = magnitude * cos(angle)
    voltage_i = magnitude * sin(angle)
    function residual!(output, states)
        environment = gui_environment(states, voltage_r, voltage_i, 1.0, wrapper, 0.0)
        for index in eachindex(model.equations)
            output[index] = gui_expression(model.equations[index], environment)
        end
    end
    solution = PowerSimulationsDynamics.NLsolve.nlsolve(residual!, values)
    PowerSimulationsDynamics.NLsolve.converged(solution) ||
        error("自定义模型 $(model.name) 的稳态初始化未收敛")
    return collect(solution.zero)
end

function add_editor_bus!(system, instance)
    number = Int(instance["bus_number"])
    parameters = get(instance, "parameters", Dict{String, Any}())
    voltage_limits = get(parameters, "voltage_limits", Dict{String, Any}())
    bus = PowerSystems.ACBus(
        number,
        string(instance["name"]),
        Bool(get(parameters, "available", true)),
        "PQ",
        Float64(get(parameters, "angle", 0.0)),
        Float64(get(parameters, "magnitude", 1.0)),
        (
            min = Float64(get(voltage_limits, "min", get(parameters, "voltage_min", 0.9))),
            max = Float64(get(voltage_limits, "max", get(parameters, "voltage_max", 1.1))),
        ),
        Float64(get(parameters, "base_voltage", 230.0)),
    )
    PowerSystems.add_component!(system, bus; skip_validation = true)
    return bus
end

parameter_group(parameters, name) = get(parameters, name, Dict{String, Any}())
parameter_value(parameters, name, default) = Float64(get(parameters, name, default))

function add_library_dynamic_generator!(system, instance, bus)
    parameters = get(instance, "parameters", Dict{String, Any}())
    name = string(instance["name"])
    base_power = parameter_value(parameters, "base_power", PowerSystems.get_base_power(system))
    available = Bool(get(parameters, "available", true))
    static = PowerSystems.ThermalStandard(nothing)
    PowerSystems.set_name!(static, name)
    PowerSystems.set_available!(static, available)
    PowerSystems.set_status!(static, available)
    PowerSystems.set_bus!(static, bus)
    static.active_power = parameter_value(parameters, "active_power", 0.5)
    static.reactive_power = parameter_value(parameters, "reactive_power", 0.0)
    static.rating = parameter_value(parameters, "rating", 1.0)
    static.active_power_limits = (min = 0.0, max = max(static.rating, static.active_power, 1.0))
    static.reactive_power_limits = (min = -max(static.rating, 1.0), max = max(static.rating, 1.0))
    static.base_power = base_power

    machine = parameter_group(parameters, "machine")
    shaft = parameter_group(parameters, "shaft")
    avr = parameter_group(parameters, "avr")
    governor = parameter_group(parameters, "prime_mover")
    pss = parameter_group(parameters, "pss")
    va_limits = parameter_group(avr, "Va_lim")
    valve_limits = parameter_group(governor, "valve_position_limits")
    dynamic = PowerSystems.DynamicGenerator(;
        name = name,
        ω_ref = parameter_value(parameters, "ω_ref", 1.0),
        machine = PowerSystems.AndersonFouadMachine(
            parameter_value(machine, "R", 0.0),
            parameter_value(machine, "Xd", 0.8979),
            parameter_value(machine, "Xq", 0.646),
            parameter_value(machine, "Xd_p", 0.2995),
            parameter_value(machine, "Xq_p", 0.646),
            parameter_value(machine, "Xd_pp", 0.23),
            parameter_value(machine, "Xq_pp", 0.4),
            parameter_value(machine, "Td0_p", 3.0),
            parameter_value(machine, "Tq0_p", 0.1),
            parameter_value(machine, "Td0_pp", 0.01),
            parameter_value(machine, "Tq0_pp", 0.033),
        ),
        shaft = PowerSystems.SingleMass(;
            H = parameter_value(shaft, "H", 3.01),
            D = parameter_value(shaft, "D", 0.0),
        ),
        avr = PowerSystems.AVRTypeII(
            parameter_value(avr, "K0", 200.0),
            parameter_value(avr, "T1", 4.0),
            parameter_value(avr, "T2", 1.0),
            parameter_value(avr, "T3", 0.006),
            parameter_value(avr, "T4", 0.06),
            parameter_value(avr, "Te", 0.0001),
            parameter_value(avr, "Tr", 0.0001),
            (
                min = parameter_value(va_limits, "min", -50.0),
                max = parameter_value(va_limits, "max", 50.0),
            ),
            parameter_value(avr, "Ae", 0.0),
            parameter_value(avr, "Be", 0.0),
            parameter_value(avr, "V_ref", 1.0),
        ),
        prime_mover = PowerSystems.TGTypeI(
            parameter_value(governor, "R", 0.02),
            parameter_value(governor, "Ts", 0.1),
            parameter_value(governor, "Tc", 0.45),
            parameter_value(governor, "T3", 0.0),
            parameter_value(governor, "T4", 12.0),
            parameter_value(governor, "T5", 50.0),
            (
                min = parameter_value(valve_limits, "min", 0.0),
                max = parameter_value(valve_limits, "max", 1.2),
            ),
            parameter_value(governor, "P_ref", 0.5),
        ),
        pss = PowerSystems.PSSFixed(parameter_value(pss, "V_pss", 0.0)),
        base_power = base_power,
    )
    PowerSystems.add_component!(system, static; skip_validation = true)
    PowerSystems.add_component!(system, dynamic, static)
    return
end

function add_library_dynamic_inverter!(system, instance, bus)
    parameters = get(instance, "parameters", Dict{String, Any}())
    name = string(instance["name"])
    base_power = parameter_value(parameters, "base_power", PowerSystems.get_base_power(system))
    available = Bool(get(parameters, "available", true))
    static = PowerSystems.ThermalStandard(nothing)
    PowerSystems.set_name!(static, name)
    PowerSystems.set_available!(static, available)
    PowerSystems.set_status!(static, available)
    PowerSystems.set_bus!(static, bus)
    static.active_power = parameter_value(parameters, "active_power", 0.5)
    static.reactive_power = parameter_value(parameters, "reactive_power", 0.0)
    static.rating = parameter_value(parameters, "rating", 1.0)
    static.active_power_limits = (min = 0.0, max = max(static.rating, static.active_power, 1.0))
    static.reactive_power_limits = (min = -max(static.rating, 1.0), max = max(static.rating, 1.0))
    static.base_power = base_power

    converter = parameter_group(parameters, "converter")
    active = parameter_group(parameters, "active_power_control")
    reactive = parameter_group(parameters, "reactive_power_control")
    inner = parameter_group(parameters, "inner_control")
    dc_source = parameter_group(parameters, "dc_source")
    frequency = parameter_group(parameters, "frequency_estimator")
    filter = parameter_group(parameters, "filter")
    dynamic = PowerSystems.DynamicInverter(;
        name = name,
        ω_ref = parameter_value(parameters, "ω_ref", 1.0),
        converter = PowerSystems.AverageConverter(;
            rated_voltage = parameter_value(converter, "rated_voltage", 690.0),
            rated_current = parameter_value(converter, "rated_current", 2.75),
        ),
        outer_control = PowerSystems.OuterControl(
            PowerSystems.ActivePowerDroop(;
                Rp = parameter_value(active, "Rp", 0.05),
                ωz = parameter_value(active, "ωz", 2 * pi * 5),
                P_ref = parameter_value(active, "P_ref", 0.5),
            ),
            PowerSystems.ReactivePowerDroop(;
                kq = parameter_value(reactive, "kq", 0.2),
                ωf = parameter_value(reactive, "ωf", 1000.0),
                V_ref = parameter_value(reactive, "V_ref", 1.0),
            ),
        ),
        inner_control = PowerSystems.VoltageModeControl(;
            kpv = parameter_value(inner, "kpv", 0.59),
            kiv = parameter_value(inner, "kiv", 736.0),
            kffv = parameter_value(inner, "kffv", 0.0),
            rv = parameter_value(inner, "rv", 0.0),
            lv = parameter_value(inner, "lv", 0.2),
            kpc = parameter_value(inner, "kpc", 1.27),
            kic = parameter_value(inner, "kic", 14.3),
            kffi = parameter_value(inner, "kffi", 0.0),
            ωad = parameter_value(inner, "ωad", 50.0),
            kad = parameter_value(inner, "kad", 0.0),
        ),
        dc_source = PowerSystems.FixedDCSource(;
            voltage = parameter_value(dc_source, "voltage", 600.0),
        ),
        freq_estimator = PowerSystems.FixedFrequency(;
            frequency = parameter_value(frequency, "frequency", 1.0),
        ),
        filter = PowerSystems.LCLFilter(;
            lf = parameter_value(filter, "lf", 0.08),
            rf = parameter_value(filter, "rf", 0.003),
            cf = parameter_value(filter, "cf", 0.074),
            lg = parameter_value(filter, "lg", 0.2),
            rg = parameter_value(filter, "rg", 0.01),
        ),
        base_power = base_power,
    )
    PowerSystems.add_component!(system, static; skip_validation = true)
    PowerSystems.add_component!(system, dynamic, static)
    return
end

function add_builtin_injection!(system, instance, bus)
    name = string(instance["name"])
    model_ref = string(instance["model_ref"])
    parameters = get(instance, "parameters", Dict{String, Any}())
    base_power = Float64(get(parameters, "base_power", PowerSystems.get_base_power(system)))
    available = Bool(get(parameters, "available", true))
    if model_ref == "StandardLoad"
        component = PowerSystems.StandardLoad(
            name = name,
            available = available,
            bus = bus,
            base_power = base_power,
            constant_active_power = Float64(get(parameters, "constant_active_power", get(parameters, "active_power", 0.0))),
            constant_reactive_power = Float64(get(parameters, "constant_reactive_power", get(parameters, "reactive_power", 0.0))),
            impedance_active_power = Float64(get(parameters, "impedance_active_power", 0.0)),
            impedance_reactive_power = Float64(get(parameters, "impedance_reactive_power", 0.0)),
            current_active_power = Float64(get(parameters, "current_active_power", 0.0)),
            current_reactive_power = Float64(get(parameters, "current_reactive_power", 0.0)),
            max_constant_active_power = Float64(get(parameters, "max_constant_active_power", 0.0)),
            max_constant_reactive_power = Float64(get(parameters, "max_constant_reactive_power", 0.0)),
            max_impedance_active_power = Float64(get(parameters, "max_impedance_active_power", 0.0)),
            max_impedance_reactive_power = Float64(get(parameters, "max_impedance_reactive_power", 0.0)),
            max_current_active_power = Float64(get(parameters, "max_current_active_power", 0.0)),
            max_current_reactive_power = Float64(get(parameters, "max_current_reactive_power", 0.0)),
        )
    elseif model_ref == "Source"
        active_limits = get(parameters, "active_power_limits", Dict{String, Any}())
        reactive_limits = get(parameters, "reactive_power_limits", Dict{String, Any}())
        component = PowerSystems.Source(
            name = name,
            available = available,
            bus = bus,
            active_power = Float64(get(parameters, "active_power", 0.0)),
            reactive_power = Float64(get(parameters, "reactive_power", 0.0)),
            active_power_limits = (
                min = Float64(get(active_limits, "min", 0.0)),
                max = Float64(get(active_limits, "max", 0.0)),
            ),
            reactive_power_limits = (
                min = Float64(get(reactive_limits, "min", 0.0)),
                max = Float64(get(reactive_limits, "max", 0.0)),
            ),
            R_th = Float64(get(parameters, "R_th", 0.0)),
            X_th = Float64(get(parameters, "X_th", 0.0)),
            internal_voltage = Float64(get(parameters, "internal_voltage", 1.0)),
            internal_angle = Float64(get(parameters, "internal_angle", 0.0)),
            base_power = base_power,
        )
    elseif model_ref == "DynamicGenerator"
        return add_library_dynamic_generator!(system, instance, bus)
    elseif model_ref == "DynamicInverter"
        return add_library_dynamic_inverter!(system, instance, bus)
    else
        error("不支持从模型库实例化拓扑设备 $model_ref")
    end
    PowerSystems.add_component!(system, component; skip_validation = true)
    return
end

function add_builtin_branch!(system, instance, from_bus, to_bus)
    from_bus === to_bus && error("$(instance["name"]) 的起点和终点不能是同一个 Bus")
    name = string(instance["name"])
    model_ref = string(instance["model_ref"])
    parameters = get(instance, "parameters", Dict{String, Any}())
    available = Bool(get(parameters, "available", true))
    arc = PowerSystems.Arc(from_bus, to_bus)
    if model_ref == "Line"
        b = parameter_group(parameters, "b")
        g = parameter_group(parameters, "g")
        angles = parameter_group(parameters, "angle_limits")
        component = PowerSystems.Line(;
            name = name,
            available = available,
            active_power_flow = parameter_value(parameters, "active_power_flow", 0.0),
            reactive_power_flow = parameter_value(parameters, "reactive_power_flow", 0.0),
            arc = arc,
            r = parameter_value(parameters, "r", 0.0),
            x = parameter_value(parameters, "x", 0.1),
            b = (
                from = parameter_value(b, "from", 0.0),
                to = parameter_value(b, "to", 0.0),
            ),
            g = (
                from = parameter_value(g, "from", 0.0),
                to = parameter_value(g, "to", 0.0),
            ),
            rating = parameter_value(parameters, "rating", 1.0),
            angle_limits = (
                min = parameter_value(angles, "min", -pi),
                max = parameter_value(angles, "max", pi),
            ),
        )
    elseif model_ref == "Transformer2W"
        shunt = parameter_group(parameters, "primary_shunt")
        component = PowerSystems.Transformer2W(;
            name = name,
            available = available,
            active_power_flow = parameter_value(parameters, "active_power_flow", 0.0),
            reactive_power_flow = parameter_value(parameters, "reactive_power_flow", 0.0),
            arc = arc,
            r = parameter_value(parameters, "r", 0.0),
            x = parameter_value(parameters, "x", 0.1),
            primary_shunt = complex(
                parameter_value(shunt, "real", 0.0),
                parameter_value(shunt, "imag", 0.0),
            ),
            rating = parameter_value(parameters, "rating", 1.0),
            base_power = parameter_value(parameters, "base_power", PowerSystems.get_base_power(system)),
        )
    else
        error("不支持从模型库实例化支路 $model_ref")
    end
    PowerSystems.add_component!(system, component; skip_validation = true)
    return
end

function build_topology_model(specification, instance, bus_numbers)
    !isempty(strip(string(get(specification, "helper_functions", "")))) &&
        error("多端口模型暂不允许辅助函数；请将数学表达式直接写入 equations! 函数体")
    states_data = get(specification, "states", Any[])
    states = Symbol.(string.(getindex.(states_data, "name")))
    ports = Symbol.(string.(getindex.(specification["ports"], "id")))
    equations, outputs = topology_assignments(
        string(specification["julia_body"]), states, ports,
    )
    parameters = Dict{Symbol, Float64}(
        Symbol(item["name"]) => Float64(item["value"])
        for item in get(specification, "parameters", Any[])
    )
    for (name, value) in get(instance, "parameters", Dict{String, Any}())
        symbol = Symbol(name)
        haskey(parameters, symbol) && (parameters[symbol] = Float64(value))
    end
    symbols = Set{Symbol}((:t, :pi, :e))
    union!(symbols, (Symbol(:x, "__", state) for state in states))
    union!(symbols, (Symbol(:p, "__", name) for name in keys(parameters)))
    union!(
        symbols,
        (
            Symbol(:u, "__", port, suffix)
            for port in ports for suffix in (:_v_r, :_v_i)
        ),
    )
    foreach(expression -> validate_gui_expression(expression, symbols), equations)
    foreach(expression -> validate_gui_expression(expression, symbols), values(outputs))
    return GUITopologyModel(
        string(instance["name"]),
        Float64(get(get(instance, "parameters", Dict{String, Any}()), "base_power", specification["base_power"])),
        states,
        Float64.(getindex.(states_data, "mass")),
        Float64.(getindex.(states_data, "initial")),
        parameters,
        equations,
        ports,
        bus_numbers,
        Int[],
        [outputs[Symbol(port, "_i_r")] for port in ports],
        [outputs[Symbol(port, "_i_i")] for port in ports],
        Dict{String, Any}("gui_model_id" => string(specification["id"])),
        PowerSystems.InfrastructureSystemsInternal(),
    )
end

function apply_editor_document!(system, editor_path, models_by_id)
    isempty(editor_path) && return
    editor = JSON3.read(read(editor_path, String), Dict{String, Any})
    instances = get(editor, "instances", Any[])
    connections = get(editor, "connections", Any[])
    buses = Dict{String, Any}(
        "bus:$(PowerSystems.get_number(bus))" => bus
        for bus in PowerSystems.get_components(PowerSystems.ACBus, system)
    )
    for instance in instances
        string(instance["kind"]) == "bus" || continue
        buses["editor:$(instance["id"])"] = add_editor_bus!(system, instance)
    end
    for instance in instances
        kind = string(instance["kind"])
        kind == "bus" && continue
        key = "editor:$(instance["id"])"
        connected = Tuple{String, Any}[]
        for connection in connections
            if string(connection["first_key"]) == key
                push!(connected, (string(connection["first_port"]), get(buses, string(connection["second_key"]), nothing)))
            elseif string(connection["second_key"]) == key
                push!(connected, (string(connection["second_port"]), get(buses, string(connection["first_key"]), nothing)))
            end
        end
        any(isnothing(last(value)) for value in connected) && error("$(instance["name"]) 引用了不存在的 Bus")
        if kind in ("line", "transformer")
            length(connected) == 2 || error("$(instance["name"]) 必须连接两个 Bus")
            port_bus = Dict(connected)
            haskey(port_bus, "from") && haskey(port_bus, "to") ||
                error("$(instance["name"]) 缺少起点或终点端口")
            add_builtin_branch!(system, instance, port_bus["from"], port_bus["to"])
        elseif kind == "custom"
            specification = get(models_by_id, string(instance["model_ref"]), nothing)
            isnothing(specification) && error("找不到自定义整体模型 $(instance["model_ref"])")
            port_bus = Dict(connected)
            port_ids = string.(getindex.(specification["ports"], "id"))
            bus_list = [port_bus[port] for port in port_ids]
            model = build_topology_model(
                specification,
                instance,
                Int[PowerSystems.get_number(bus) for bus in bus_list],
            )
            parameters = get(instance, "parameters", Dict{String, Any}())
            static = PowerSystems.StandardLoad(
                string(instance["name"]),
                Bool(get(parameters, "available", true)),
                first(bus_list),
                PowerSystems.get_base_power(system),
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            )
            PowerSystems.add_component!(system, static; skip_validation = true)
            PowerSystems.add_component!(system, model, static)
        else
            length(connected) == 1 || error("$(instance["name"]) 必须连接且只能连接一个 Bus")
            add_builtin_injection!(system, instance, last(only(connected)))
        end
    end
    return
end

function load_gui_models!(system, path, editor_path = "", editor_structure = "")
    data = isempty(path) ? Dict{String, Any}("schema_version" => 1, "models" => Any[]) :
        JSON3.read(read(path, String), Dict{String, Any})
    get(data, "schema_version", 0) == 1 || error("自定义模型仅支持 schema_version 1")
    models_by_id = Dict(string(item["id"]) => item for item in get(data, "models", Any[]))
    apply_editor_document!(system, editor_path, models_by_id)
    structure_lines = String[]
    for specification in get(data, "models", Any[])
        model_kind = string(get(specification, "model_kind", "equation"))
        push!(structure_lines, "model_kind|$(specification["id"])|$model_kind")
        push!(structure_lines, "attachment|$(get(specification, "static_injection", ""))")
        push!(structure_lines, "base|$(get(specification, "base_model", ""))|$(get(specification, "base_category", ""))")
        for port in get(specification, "ports", Any[])
            push!(
                structure_lines,
                "port|$(port["id"])|$(get(port, "domain", "AC_BUS"))|$(get(port, "role", "bidirectional"))|$(get(port, "required", true))",
            )
        end
        for state_value in get(specification, "states", Any[])
            push!(
                structure_lines,
                "state|$(state_value["name"])|$(state_value["mass"])|$(get(state_value, "rhs", ""))",
            )
        end
        for parameter in get(specification, "parameters", Any[])
            push!(structure_lines, "parameter|$(parameter["name"])")
        end
        if model_kind == "topology"
            push!(structure_lines, "body|$(get(specification, "julia_body", ""))")
            push!(structure_lines, "helpers|$(get(specification, "helper_functions", ""))")
            continue
        end
        model_kind == "equation" || continue
        name = string(specification["static_injection"])
        static = only(
            component for component in PowerSystems.get_components(PowerSystems.StaticInjection, system)
            if PowerSystems.get_name(component) == name
        )
        isnothing(PowerSystems.get_dynamic_injector(static)) ||
            error("设备 $name 已绑定动态模型，不能再绑定自定义模型")
        states_data = specification["states"]
        push!(structure_lines, "model|$name")
        for state in states_data
            push!(
                structure_lines,
                "state|$(state["name"])|$(state["mass"])|$(state["rhs"])",
            )
        end
        for parameter in specification["parameters"]
            push!(structure_lines, "parameter|$(parameter["name"])")
        end
        push!(structure_lines, "output|i_r|$(specification["outputs"]["i_r"])")
        push!(structure_lines, "output|i_i|$(specification["outputs"]["i_i"])")
        parameters = Dict{Symbol, Float64}(
            Symbol(item["name"]) => Float64(item["value"])
            for item in specification["parameters"]
        )
        equations = Meta.parse.(getindex.(states_data, "rhs"))
        current_r_expression = Meta.parse(specification["outputs"]["i_r"])
        current_i_expression = Meta.parse(specification["outputs"]["i_i"])
        symbols = union(
            GUI_INPUTS,
            Set(Symbol.(getindex.(states_data, "name"))),
            Set(keys(parameters)),
        )
        foreach(expression -> validate_gui_expression(expression, symbols), equations)
        validate_gui_expression(current_r_expression, symbols)
        validate_gui_expression(current_i_expression, symbols)
        active_power = static isa PowerSystems.StandardLoad ?
            PowerSimulationsDynamics.get_total_p(static) :
            PowerSystems.get_active_power(static)
        reactive_power = static isa PowerSystems.StandardLoad ?
            PowerSimulationsDynamics.get_total_q(static) :
            PowerSystems.get_reactive_power(static)
        model = GUIEquationModel(
            name,
            Float64(specification["base_power"]),
            Symbol.(getindex.(states_data, "name")),
            Float64.(getindex.(states_data, "mass")),
            Float64.(getindex.(states_data, "initial")),
            Symbol(specification["initialization"]),
            parameters,
            equations,
            current_r_expression,
            current_i_expression,
            Float64(active_power),
            Float64(reactive_power),
            Float64(PowerSystems.get_magnitude(PowerSystems.get_bus(static))),
            1.0,
            Dict{String, Any}(
                "gui_model_id" => string(specification["id"]),
                "display_name" => string(specification["name"]),
            ),
            PowerSystems.InfrastructureSystemsInternal(),
        )
        PowerSystems.set_dynamic_injector!(static, model)
    end
    source = join(structure_lines, '\n') * editor_structure
    return isempty(source) ? "" : bytes2hex(SHA.sha256(codeunits(source)))
end

struct JobCancelled <: Exception end

function protocol(kind, key, value)
    text = replace(string(value), '\n' => ' ', '\r' => ' ', '\t' => ' ')
    lock(OUTPUT_LOCK) do
        println(kind, '\t', key, '\t', text)
        flush(stdout)
    end
end

event(phase, message) = protocol("PSID_EVENT", phase, message)
state(value, message) = protocol("PSID_STATE", value, message)
info(key, value) = protocol("PSID_INFO", key, value)
check_cancelled() = CANCEL_REQUESTED[] && throw(JobCancelled())

function csv_cell(value)
    text = string(value)
    return occursin(r"[\",\n\r]", text) ? "\"" * replace(text, "\"" => "\"\"") * "\"" : text
end

function add_series!(headers, columns, skipped, label, f, expected_length)
    try
        _, values = f()
        values = collect(values)
        length(values) == expected_length || error("时间点数量不一致")
        push!(headers, label)
        push!(columns, values)
    catch error_value
        reason = first(split(sprint(showerror, error_value), '\n'))
        push!(skipped, "$label: $reason")
    end
end

function export_all(results, system, output_file, metadata_file)
    solution = PowerSimulationsDynamics.get_solution(results)
    keep = unique(i -> solution.t[i], eachindex(solution.t))
    times = collect(solution.t[keep])
    raw = Array(solution)[:, keep]
    variable_names = ["x[$index]" for index in axes(raw, 1)]

    bus_lookup = PowerSimulationsDynamics.get_bus_lookup(results)
    bus_count = PowerSimulationsDynamics.get_bus_count(results)
    for (number, index) in bus_lookup
        variable_names[index] = "bus/$number/voltage_real"
        variable_names[index + bus_count] = "bus/$number/voltage_imag"
    end
    for (device, states) in PowerSimulationsDynamics.get_global_index(results)
        for (state, index) in states
            variable_names[index] = "device/$device/state/$state"
        end
    end

    headers = ["time"]
    columns = Any[times]
    for index in axes(raw, 1)
        push!(headers, variable_names[index])
        push!(columns, collect(raw[index, :]))
    end
    skipped = String[]
    n = length(times)

    for bus in PowerSystems.get_components(PowerSystems.ACBus, system)
        number = PowerSystems.get_number(bus)
        add_series!(headers, columns, skipped, "bus/$number/voltage_magnitude", () -> PowerSimulationsDynamics.get_voltage_magnitude_series(results, number), n)
        add_series!(headers, columns, skipped, "bus/$number/voltage_angle", () -> PowerSimulationsDynamics.get_voltage_angle_series(results, number), n)
    end

    for device in PowerSystems.get_components(PowerSystems.StaticInjection, system)
        name = PowerSystems.get_name(device)
        prefix = "injection/$name"
        add_series!(headers, columns, skipped, "$prefix/current_real", () -> PowerSimulationsDynamics.get_real_current_series(results, name), n)
        add_series!(headers, columns, skipped, "$prefix/current_imag", () -> PowerSimulationsDynamics.get_imaginary_current_series(results, name), n)
        add_series!(headers, columns, skipped, "$prefix/active_power", () -> PowerSimulationsDynamics.get_activepower_series(results, name), n)
        add_series!(headers, columns, skipped, "$prefix/reactive_power", () -> PowerSimulationsDynamics.get_reactivepower_series(results, name), n)
        add_series!(headers, columns, skipped, "$prefix/frequency", () -> PowerSimulationsDynamics.get_frequency_series(results, name), n)
        add_series!(headers, columns, skipped, "$prefix/field_current", () -> PowerSimulationsDynamics.get_field_current_series(results, name), n)
        add_series!(headers, columns, skipped, "$prefix/field_voltage", () -> PowerSimulationsDynamics.get_field_voltage_series(results, name), n)
        add_series!(headers, columns, skipped, "$prefix/pss_output", () -> PowerSimulationsDynamics.get_pss_output_series(results, name), n)
        add_series!(headers, columns, skipped, "$prefix/mechanical_torque", () -> PowerSimulationsDynamics.get_mechanical_torque_series(results, name), n)
        if device isa PowerSystems.Source
            add_series!(headers, columns, skipped, "$prefix/source_current_real", () -> PowerSimulationsDynamics.get_source_real_current_series(results, name), n)
            add_series!(headers, columns, skipped, "$prefix/source_current_imag", () -> PowerSimulationsDynamics.get_source_imaginary_current_series(results, name), n)
        end
    end

    for branch in PowerSystems.get_components(PowerSystems.ACBranch, system)
        name = PowerSystems.get_name(branch)
        prefix = "branch/$name"
        add_series!(headers, columns, skipped, "$prefix/current_real", () -> PowerSimulationsDynamics.get_real_current_branch_flow(results, name), n)
        add_series!(headers, columns, skipped, "$prefix/current_imag", () -> PowerSimulationsDynamics.get_imaginary_current_branch_flow(results, name), n)
        for location in (:from, :to)
            add_series!(headers, columns, skipped, "$prefix/$location/active_power", () -> PowerSimulationsDynamics.get_activepower_branch_flow(results, name, location), n)
            add_series!(headers, columns, skipped, "$prefix/$location/reactive_power", () -> PowerSimulationsDynamics.get_reactivepower_branch_flow(results, name, location), n)
        end
    end

    partial = output_file * ".partial"
    open(partial, "w") do io
        println(io, join(csv_cell.(headers), ','))
        for row in eachindex(times)
            println(io, join((csv_cell(column[row]) for column in columns), ','))
        end
    end
    mv(partial, output_file; force = true)
    open(metadata_file, "w") do io
        TOML.print(io, Dict(
            "schema_version" => 1,
            "row_count" => length(times),
            "column_count" => length(headers),
            "columns" => headers,
            "skipped_derived_series" => skipped,
        ))
    end
    return length(headers), length(skipped)
end

function precompile_backend()
    event("precompile", "正在预编译 Julia 环境")
    Pkg.instantiate()
    Pkg.precompile()
    event("complete", "Julia 环境预编译完成")
end

function resolve_compiled_network(
    config,
    model,
    system,
    frequency_reference;
    force_rebuild = false,
    custom_fingerprint = "",
)
    project = config["project"]
    fingerprint = PowerSimulationsDynamics.network_fingerprint(
        model,
        system;
        frequency_reference = frequency_reference,
    )
    if !isempty(custom_fingerprint)
        fingerprint = bytes2hex(
            SHA.sha256(codeunits(fingerprint * "|gui-models|" * custom_fingerprint)),
        )
    end
    info("current_fingerprint", fingerprint)
    manifest = TOML.parsefile(joinpath(project, "manifest.toml"))
    project_fingerprint = get(manifest, "fingerprint", "")
    info("project_fingerprint", project_fingerprint)

    if force_rebuild
        pop!(MEMORY_NETWORKS, fingerprint, nothing)
        info("cache_status", "正在重新构建")
        event("cache_miss", "已请求重新构建当前网络计算图")
        return nothing, fingerprint
    end

    if haskey(MEMORY_NETWORKS, fingerprint)
        info("cache_status", "内存缓存命中")
        event("cache_hit", "已复用常驻后端中的网络计算图")
        return MEMORY_NETWORKS[fingerprint], fingerprint
    end
    if project_fingerprint == fingerprint && isfile(joinpath(project, "compiled", "network.bin"))
        compiled = PowerSimulationsDynamics.load_compiled_network(project)
        MEMORY_NETWORKS[fingerprint] = compiled
        info("cache_status", "导出项目缓存命中")
        event("cache_hit", "已载入导出项目中的网络计算图")
        return compiled, fingerprint
    end

    cache_directory = joinpath(config["network_cache"], fingerprint)
    if isfile(joinpath(cache_directory, "compiled", "network.bin"))
        compiled = PowerSimulationsDynamics.load_compiled_network(cache_directory)
        MEMORY_NETWORKS[fingerprint] = compiled
        info("cache_status", "应用缓存命中")
        event("cache_hit", "已载入应用缓存中的网络计算图")
        return compiled, fingerprint
    end
    info("cache_status", project_fingerprint == fingerprint ? "缓存缺失" : "项目缓存失效")
    event("cache_miss", "计算图缓存未命中，本次将构建并保存")
    return nothing, fingerprint
end

function build_simulation(config; simulation_directory = nothing, force_rebuild = false)
    check_cancelled()
    system_file = config["system_file"]
    if isnothing(simulation_directory)
        result_directory = joinpath(config["output_directory"], config["output_name"])
        mkpath(result_directory)
        simulation_directory = joinpath(result_directory, "simulation")
        mkpath(simulation_directory)
    end

    event("load", "正在载入 PowerSystems.System")
    system = PowerSystems.System(system_file; runchecks = false)
    custom_models_file = get(config, "custom_models_file", "")
    editor_model_file = get(config, "editor_model_file", "")
    custom_fingerprint = load_gui_models!(
        system,
        custom_models_file,
        editor_model_file,
        get(config, "editor_structure_fingerprint", ""),
    )
    !isempty(custom_fingerprint) && info("custom_models", "已装配自定义微分方程模型")
    model_name = config["model"]
    model = model_name == "MassMatrixModel" ? PowerSimulationsDynamics.MassMatrixModel : PowerSimulationsDynamics.ResidualModel
    frequency_reference = config["frequency_reference"] == "ConstantFrequency" ? PowerSimulationsDynamics.ConstantFrequency() : PowerSimulationsDynamics.ReferenceBus()
    info("model", model_name)
    info("frequency_reference", config["frequency_reference"])

    build_kwargs = Dict{Symbol, Any}(
        :initialize_simulation => config["initialize_simulation"],
        :frequency_reference => frequency_reference,
        :disable_timer_outputs => config["disable_timer_outputs"],
    )
    compiled, fingerprint = resolve_compiled_network(
        config,
        model,
        system,
        frequency_reference,
        force_rebuild = force_rebuild,
        custom_fingerprint = custom_fingerprint,
    )
    if isnothing(compiled)
        mkpath(config["network_cache"])
        if !force_rebuild && isempty(custom_fingerprint)
            build_kwargs[:network_cache] = config["network_cache"]
        end
    else
        build_kwargs[:compiled_network] = compiled
    end
    check_cancelled()
    event("initialize", "正在绑定参数并初始化仿真")
    simulation = PowerSimulationsDynamics.Simulation(
        model,
        system,
        simulation_directory,
        (Float64(config["start_time"]), Float64(config["end_time"]));
        build_kwargs...,
    )
    simulation.status == PowerSimulationsDynamics.BUILT || error("仿真构建失败：$(simulation.status)")
    if isnothing(compiled)
        check_cancelled()
        cached_directory = joinpath(config["network_cache"], fingerprint)
        if force_rebuild || !isempty(custom_fingerprint)
            compiled = PowerSimulationsDynamics.compile_network(simulation)
            PowerSimulationsDynamics.save_compiled_network(compiled, cached_directory)
            MEMORY_NETWORKS[fingerprint] = compiled
        else
            MEMORY_NETWORKS[fingerprint] = PowerSimulationsDynamics.load_compiled_network(cached_directory)
        end
        info("cache_status", "已构建并缓存")
        event("cache_saved", "网络计算图已保存到应用缓存")
    end
    return simulation, system, simulation_directory
end

function prepare_network(config_path; force_rebuild = false)
    config = TOML.parsefile(config_path)
    mktempdir(prefix = "psid-viewer-prepare-") do directory
        build_simulation(
            config;
            simulation_directory = directory,
            force_rebuild = force_rebuild,
        )
    end
    event("complete", "当前网络计算图已准备完成")
end

function run_simulation(config_path)
    config = TOML.parsefile(config_path)
    simulation, system, _ = build_simulation(config)
    check_cancelled()
    output_name = config["output_name"]
    result_directory = joinpath(config["output_directory"], output_name)

    solver_name = config["solver"]
    solver = if solver_name == "IDA"
        Sundials.IDA()
    elseif solver_name == "Rodas5"
        OrdinaryDiffEq.Rodas5()
    else
        OrdinaryDiffEq.Rodas4()
    end
    solve_kwargs = Dict{Symbol, Any}(:enable_progress_bar => false)
    for key in ("saveat", "dtmax", "abstol", "reltol")
        value = get(config, key, 0.0)
        value > 0 && (solve_kwargs[Symbol(key)] = Float64(value))
    end
    get(config, "maxiters", 0) > 0 && (solve_kwargs[:maxiters] = Int(config["maxiters"]))
    solve_kwargs[:adaptive] = config["adaptive"]
    cancel_callback = PowerSimulationsDynamics.SciMLBase.DiscreteCallback(
        (_, _, _) -> CANCEL_REQUESTED[],
        integrator -> PowerSimulationsDynamics.SciMLBase.terminate!(integrator);
        save_positions = (false, false),
    )
    push!(simulation.callbacks, cancel_callback)

    event("run", "正在运行仿真")
    status = PowerSimulationsDynamics.execute!(simulation, solver; solve_kwargs...)
    check_cancelled()
    status == PowerSimulationsDynamics.SIMULATION_FINALIZED || error("仿真失败：$status")
    event("export", "正在导出全部可用数据")
    output_file = joinpath(result_directory, output_name * ".csv")
    metadata_file = joinpath(result_directory, "metadata.toml")
    columns, skipped = export_all(PowerSimulationsDynamics.read_results(simulation), system, output_file, metadata_file)
    saved_config = copy(config)
    saved_config["system_file"] = "parameters.json"
    open(joinpath(result_directory, "simulation.toml"), "w") do stream
        TOML.print(stream, saved_config; sorted = true)
    end
    cp(config["system_file"], joinpath(result_directory, "parameters.json"); force = true)
    info("result_file", output_file)
    info("metadata_file", metadata_file)
    event("complete", "完成：$output_file（$columns 列，$skipped 个不适用派生量）")
end

function start_job(action, f)
    accepted = lock(JOB_LOCK) do
        JOB_BUSY[] && return false
        JOB_BUSY[] = true
        return true
    end
    if !accepted
        event("error", "Julia 后端正忙，无法执行 $action")
        return
    end
    CANCEL_REQUESTED[] = false
    state("Busy", action)
    Threads.@spawn begin
        try
            f()
        catch error_value
            if error_value isa JobCancelled
                event("cancelled", "当前任务已终止")
            else
                showerror(stderr, error_value, catch_backtrace())
                println(stderr)
                event("error", sprint(showerror, error_value))
            end
        finally
            lock(JOB_LOCK) do
                JOB_BUSY[] = false
            end
            if SHUTDOWN_REQUESTED[]
                exit(0)
            else
                state("Ready", "Julia 后端已就绪")
            end
        end
    end
end

function serve()
    info("pid", getpid())
    info("julia_version", VERSION)
    info("psid_version", Base.pkgversion(PowerSimulationsDynamics))
    state("Ready", "Julia 后端已就绪")
    for line in eachline(stdin)
        parts = split(line, '\t'; limit = 2)
        command = parts[1]
        argument = length(parts) == 2 ? parts[2] : ""
        if command == "PRECOMPILE"
            start_job("正在预编译 Julia 环境", precompile_backend)
        elseif command == "PREPARE"
            start_job("正在准备网络计算图", () -> prepare_network(argument))
        elseif command == "REBUILD"
            start_job("正在重新构建网络计算图", () -> prepare_network(argument; force_rebuild = true))
        elseif command == "RUN"
            start_job("正在运行仿真", () -> run_simulation(argument))
        elseif command == "CANCEL"
            if JOB_BUSY[]
                CANCEL_REQUESTED[] = true
                event("cancel", "已请求终止当前任务")
            end
        elseif command == "PING"
            state(JOB_BUSY[] ? "Busy" : "Ready", JOB_BUSY[] ? "Julia 后端正忙" : "Julia 后端已就绪")
        elseif command == "SHUTDOWN"
            SHUTDOWN_REQUESTED[] = true
            if JOB_BUSY[]
                CANCEL_REQUESTED[] = true
            else
                break
            end
        else
            event("error", "未知后端命令：$command")
        end
    end
end

if length(ARGS) == 1 && ARGS[1] == "serve"
    serve()
else
    error("psid_backend.jl 必须以 serve 模式启动")
end
