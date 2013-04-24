<%block name="instance_types_block">
    %for instance_type in instance_types:
        %if instance_type[0] == "group":
             <optgroup label="${instance_type[1]}">
             %for sub_type in instance_type[2]:
                <option value='${sub_type[0]}' title="${'' if len(sub_type) <= 2 else sub_type[2]}">${sub_type[1]}</option>
             %endfor
             </optgroup>
        %else:
            %if instance_type[0] == "":
                <option value='${master_instance_type}'>Same as Master (${master_instance_type})</option>
            %else:
                <option value='${instance_type[0]}' title="${'' if len(instance_type) <= 2 else instance_type[2]}">${instance_type[1]}</option>
            %endif
        %endif
    %endfor
</%block>
