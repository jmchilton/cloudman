<%inherit file="../amazon/instance_types.mako"/>
<%block name="instance_types">
	 <option value='${master_instance_type}'>Same as Master (${master_instance_type})</option>
     <option value='m1.small'>Small</option>
     <option value='m1.medium'>Medium</option>
     <option value='m1.xlarge'>Extra Large</option>
     <option value='m1.xxlarge'>Extra Extra Large</option>
</%block>