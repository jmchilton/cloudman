<%
from routes import url_for
%>

## This needs to be on the first line, otherwise IE6 goes into quirks mode
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">

## Default title
<%def name="title()">Galaxy Cloud</%def>

## Default stylesheets
<%def name="stylesheets()">
  <link href="${h.url_for('/static/style/base.css')}" rel="stylesheet" type="text/css" />
  <link href="${h.url_for('/static/style/masthead.css')}" rel="stylesheet" type="text/css" />
</%def>

## Default javascripts
<%def name="javascripts()">
  <!--[if lt IE 7]>
  <script type='text/javascript' src="/static/scripts/IE7.js"></script>
  <script type='text/javascript' src="/static/scripts/IE8.js"></script>
  <script type='text/javascript' src="/static/scripts/ie7-recalc.js"></script>
  <![endif]-->
  <script type='text/javascript' src="${h.url_for('/static/scripts/jQuery-1.4.2.js')}"></script>
  <script type='text/javascript' src="${h.url_for('/static/scripts/livevalidation_standalone.compressed.js')}"></script>
</%def>

## Default late-load javascripts
<%def name="late_javascripts()">
</%def>
    
## Masthead
<%def name="masthead()">
  <table width="100%" cellspacing="0" border="0">
    <tr valign="middle">
      <td width="26px">
        <a target="_blank" href="http://usegalaxy.org/cloud">
        <img border="0" src="${h.url_for('/static/images/galaxyIcon_noText.png')}"></a>
      </td>
      <td align="left" valign="middle"><div class="pageTitle">Galaxy Cloudman</div></td>
      <td align="right" valign="middle">
      %if CM_url:
        <span id='cm_update_message'>
			  There is a <span style="color:#5CBBFF">new version</span> of CloudMan:
			  <a target="_blank" href="${CM_url}">What's New</a> | 
			  <a id='update_cm' href="#">Update CloudMan</a>
	          &nbsp;&nbsp;&nbsp;
        </span>
         <span style='display:none' id="update_reboot_now"><a href="#">Restart cluster now?</a></span>&nbsp;&nbsp;&nbsp;
      %endif
		  Info: <a href="mailto:galaxy-bugs@bx.psu.edu">report bugs</a>
        | <a target="_blank" href="http://usegalaxy.org/cloud">wiki</a>                  
        | <a target="_blank" href="http://screencast.g2.bx.psu.edu/cloud/">screencast</a>
        &nbsp;
      </td>
    </tr>
  </table>
</%def>

## Document
<html lang="en">
  <head>
    <title>${self.title()}</title>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
    ${self.javascripts()}
    ${self.stylesheets()}
  </head>
  <body>
    ## Background displays first
    <div id="background"></div>
    ## Layer masthead iframe over background
    <div id="masthead">
      ${self.masthead()}
    </div>
    ## Display main CM body
    <div id="main_body">
      ${self.main_body()}
    </div>
    ## Allow other body level elements
    ${next.body()}
  </body>
  ## Scripts can be loaded later since they progressively add features to
  ## the panels, but do not change layout
  ${self.late_javascripts()}
</html>
