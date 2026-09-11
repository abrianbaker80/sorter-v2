<script lang="ts">
	type InputType = 'text' | 'number' | 'password' | 'email' | 'search';

	let {
		id,
		type = 'text',
		value = $bindable(''),
		placeholder = '',
		step,
		min,
		max,
		disabled = false,
		readonly = false,
		class: className = '',
		onchange,
		oninput
	}: {
		id?: string;
		type?: InputType;
		value?: string | number;
		placeholder?: string;
		step?: string | number;
		min?: string | number;
		max?: string | number;
		disabled?: boolean;
		readonly?: boolean;
		class?: string;
		onchange?: (event: Event) => void;
		oninput?: (event: Event) => void;
	} = $props();
</script>

<input
	{id}
	{type}
	bind:value
	{placeholder}
	{step}
	{min}
	{max}
	{disabled}
	{readonly}
	{onchange}
	{oninput}
	class="setup-control w-full px-3 py-2 text-sm text-text disabled:cursor-not-allowed disabled:opacity-60 {className}"
/>
