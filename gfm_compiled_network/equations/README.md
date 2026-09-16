# Network equation hierarchy

Graph fingerprint: `3e187760fa37a7d5f9d1403105c1610752c79d90a7b29f89a76454a0006db59e`

The TOML index is machine-readable. The copied Julia files contain the human-readable equation-method families used by the selected component types.

## Components

- `network`: `current_injection_balance` — network_model (`models/network_model.jl`)
- `system`: `system_model` — system_mass_matrix!, system_residual! (`models/system.jl`)
- `injections/generator-3-1`: `DynamicGenerator{AndersonFouadMachine, SingleMass, AVRTypeII, TGTypeI, PSSFixed}` — device! (`models/device.jl`)
- `injections/generator-3-1/avr`: `AVRTypeII` — mdl_avr_ode! (`models/generator_models/avr_models.jl`)
- `injections/generator-3-1/machine`: `AndersonFouadMachine` — mdl_machine_ode! (`models/generator_models/machine_models.jl`)
- `injections/generator-3-1/prime_mover`: `TGTypeI` — mdl_tg_ode! (`models/generator_models/tg_models.jl`)
- `injections/generator-3-1/pss`: `PSSFixed` — mdl_pss_ode! (`models/generator_models/pss_models.jl`)
- `injections/generator-3-1/shaft`: `SingleMass` — mdl_shaft_ode! (`models/generator_models/shaft_models.jl`)
- `injections/generator-1-1`: `DynamicGenerator{AndersonFouadMachine, SingleMass, AVRTypeII, TGTypeI, PSSFixed}` — device! (`models/device.jl`)
- `injections/generator-1-1/avr`: `AVRTypeII` — mdl_avr_ode! (`models/generator_models/avr_models.jl`)
- `injections/generator-1-1/machine`: `AndersonFouadMachine` — mdl_machine_ode! (`models/generator_models/machine_models.jl`)
- `injections/generator-1-1/prime_mover`: `TGTypeI` — mdl_tg_ode! (`models/generator_models/tg_models.jl`)
- `injections/generator-1-1/pss`: `PSSFixed` — mdl_pss_ode! (`models/generator_models/pss_models.jl`)
- `injections/generator-1-1/shaft`: `SingleMass` — mdl_shaft_ode! (`models/generator_models/shaft_models.jl`)
- `injections/generator-2-1`: `DynamicInverter{AverageConverter, OuterControl{ActivePowerDroop, ReactivePowerDroop}, VoltageModeControl, FixedDCSource, FixedFrequency, LCLFilter, Nothing}` — device! (`models/device.jl`)
- `injections/generator-2-1/converter`: `AverageConverter` — mdl_converter_ode! (`models/inverter_models/converter_models.jl`)
- `injections/generator-2-1/dc_source`: `FixedDCSource` — mdl_DCside_ode! (`models/inverter_models/DCside_models.jl`)
- `injections/generator-2-1/filter`: `LCLFilter` — mdl_filter_ode! (`models/inverter_models/filter_models.jl`)
- `injections/generator-2-1/freq_estimator`: `FixedFrequency` — mdl_freq_estimator_ode! (`models/inverter_models/frequency_estimator_models.jl`)
- `injections/generator-2-1/inner_control`: `VoltageModeControl` — mdl_inner_ode! (`models/inverter_models/inner_control_models.jl`)
- `injections/generator-2-1/outer_control`: `OuterControl{ActivePowerDroop, ReactivePowerDroop}` — mdl_outer_ode! (`models/inverter_models/outer_control_models.jl`)
- `injections/generator-2-1/outer_control/active_power_control`: `ActivePowerDroop` — mdl_outer_ode! (`models/inverter_models/outer_control_models.jl`)
- `injections/generator-2-1/outer_control/reactive_power_control`: `ReactivePowerDroop` — mdl_outer_ode! (`models/inverter_models/outer_control_models.jl`)
- `injections/load51`: `StandardLoad` — mdl_zip_load!, device! (`models/load_models.jl`)
- `injections/load61`: `StandardLoad` — mdl_zip_load!, device! (`models/load_models.jl`)
- `injections/load81`: `StandardLoad` — mdl_zip_load!, device! (`models/load_models.jl`)

## Source files

- `sources/models/device.jl`
- `sources/models/generator_models/avr_models.jl`
- `sources/models/generator_models/machine_models.jl`
- `sources/models/generator_models/pss_models.jl`
- `sources/models/generator_models/shaft_models.jl`
- `sources/models/generator_models/tg_models.jl`
- `sources/models/inverter_models/DCside_models.jl`
- `sources/models/inverter_models/converter_models.jl`
- `sources/models/inverter_models/filter_models.jl`
- `sources/models/inverter_models/frequency_estimator_models.jl`
- `sources/models/inverter_models/inner_control_models.jl`
- `sources/models/inverter_models/outer_control_models.jl`
- `sources/models/load_models.jl`
- `sources/models/network_model.jl`
- `sources/models/system.jl`
