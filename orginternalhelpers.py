
import sublime
import sublime_plugin
import datetime
import re
from pathlib import Path
import os
import fnmatch
import logging
import sys
import traceback
import yaml
import json
#from jsoncomment import JsonComment
import ast

varre = re.compile(r'var\((?P<name>[^)]+)\)')
colorre = re.compile(r'#(?P<r>[A-Fa-f0-9][A-Fa-f0-9])(?P<g>[A-Fa-f0-9][A-Fa-f0-9])(?P<b>[A-Fa-f0-9][A-Fa-f0-9])(?P<a>[A-Fa-f0-9][A-Fa-f0-9])?')

template = """
    - match: '{{{{beginsrc}}}}(({match})\s*)'
      captures:
        1: constant.other orgmode.fence.sourceblock
        2: orgmode.fence.sourceblock
        3: keyword orgmode.fence.language
        4: orgmode.fence.sourceblock
      embed: scope:{source}
      escape: '{{{{endsrc}}}}'
      embed_scope: markup.raw.block orgmode.raw.block
      escape_captures:
        1: constant.other orgmode.fence.sourceblock"""


introBlock = """
    // GENERATED: By OrgExtended
    //
    // The generator adds a subset of the orgmode specific scopes.
    // The scopes that it has added tend to play an important role
    // in making an orgmode buffer operational.
    //
    // That said, orgmode offers a wide variety of syntax elements for
    // you to style as needed. Please see blow for more information
    // on some of these scopes.
    //
    // The preamble scope is one of the more important scopes. In the
    // future I hope to produce some ligature fonts that will make the preamble
    // scope a thing of the past. For now, the preamble is the scope that hides
    // leading stars in your buffer. I find those visually disturbing and
    // appreciate working with them being invisible.
    //
    // The preamble used the pre-defined background color of your theme to
    // ensure the stars are invisible.
"""

# TODO Create blocks for each of the relevant blocks of markers
#      Create a set of useful markers with comments about how you can customize them
commentBlock = """
	// GENERATED: By Org Extended
    // 
    // The generator has added a bunch of useful extensions to the color scheme
    // That said there is much more than can be done by you to tweak your scheme
    // to your hearts content. The following comment block is here to give you
    // ideas of what is possible.
    //
    // On a windows box type Ctrl + Alt + Shift + P
    // to get a view of what scopes are in play on the thing you want to style
    // This table can help you tweak what you would like to see change
    //
	//	{
	//		"scope": "orgmode.break",
	//		"foreground": "#ed188a",
	//	},
	//	{
	//		"scope": "orgmode.page",
	//		"foreground": "#f5eebf",
	//	},
	//	{
	//		"scope": "orgmode.headline",
	//		"foreground": "#14adfa",
	//		"background": "#172822",
	//		"font_style": "bold italic",
	//	},
	//	{
	//		"scope": "orgmode.headline2",
	//		"background": "#172822",			
	//		"foreground": "#bb86fc",
	//		"font_style": "bold underline",
	//	},
	//	{
	//		"scope": "orgmode.headline3",
	//		"foreground": "#03dac5",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.headline4",
	//		"foreground": "#dfe6a1",
	//		"font_style": "italic",
	//	},
	//	{
	//		"scope": "orgmode.headline5",
	//		"foreground": "#018786",
	//		"font_style": "italic",
	//	},
	//	{
	//		"scope": "orgmode.headline6",
	//		"foreground": "#afe3de",
	//		"font_style": "italic",
	//	},
	//	// Links can target these
	//	{
	//		"scope": "orgmode.target",
	//		"foreground": "#7c004a",
	//		"font_style": "italic",
	//	},
	//	// Links can target these
	//	{
	//		"scope": "orgmode.target.bracket",
	//		"foreground": "#7c004a",
	//		"font_style": "bold",
	//	},
	//	// [[LINK]] This is the entire link block
	//	{
	//		"scope": "orgmode.link",
	//		"foreground": "#3cd7fa",
	//		"font_style": "",
	//	},
	//	{
	//		"scope": "orgmode.link.href",
	//		"foreground": "#9999aa",
	//		"font_style": "italic",
	//	},
	//	{
	//		"scope": "orgmode.link.text",
	//		"foreground": "#4ce7fd",
	//		"font_style": "bold",
	//	},
	//  // Special coloring for email addresses
	//	{
	//		"scope": "orgmode.email",
	//		"foreground": "#a188b3",
	//		"font_style": "italic",
	//	},
	//	{
	//		"scope": "orgmode.deadline",
	//		"foreground": "#d1771d",
	//		"font_style": "italic",
	//	},
	//	{
	//		"scope": "orgmode.scheduled",
	//		"foreground": "#d1771d",
	//		"font_style": "italic",
	//	},
	//	// #+PRIORITIES: A B C
	//	{
	//		"scope": "orgmode.controltag",
	//		"foreground": "#aaaaaa",
	//	},
	//	// controltag.text also exists
	//	{
	//		"scope": "orgmode.controltag.tag",
	//		"foreground": "#aaaaaa",
	//		"font_style": "italic",
	//	},
	//	// < DATETIME >
	//	{
	//		"scope": "orgmode.datetime",
	//		"foreground": "#b0a497",
	//	},
	//	// [ DATETIME ]
	//	{
	//		"scope": "orgmode.unscheddatetime",
	//		"foreground": "#b0a497",
	//	},
	//	// CLOSED: SCHEDULED: DEADLINE:		
	//	{
	//		"scope": "orgmode.statekeyword",
	//		"foreground": "#d1771d",
	//		"font_style": "italic",
	//	},
	//	{
	//		"scope": "orgmode.checkbox",
	//		"foreground": "#c9be7b",
	//	},
	//	{
	//		"scope": "orgmode.checkbox.checked",
	//		"foreground": "#00FF00",
	//	},
	//	{
	//		"scope": "orgmode.checkbox.blocked",
	//		"foreground": "#FF0000",
	//	},
	//	{
	//		"scope": "orgmode.tags",
	//		"foreground": "#ded49b",
	//	},
	//	{
	//		"scope": "orgmode.tags.headline",
	//		"foreground": "#deff9b",
	//	},		
	//	{
	//		"scope": "orgmode.tack",
	//		"foreground": "#c993c4",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.numberedlist",
	//		"foreground": "#c993c4",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.definition",
	//		"foreground": "#A2E8E4",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.definition.marker",
	//		"foreground": "#E1A2E8",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.follow_up",
	//		"foreground": "#FF0000",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.fence",
	//		"background": "#322830",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.fence.language",
	//		"background": "#322830",
	//		"foreground": "#f1bff2",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.raw.block",
	//		"background": "#252520",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.table.block",
	//		"background": "#272828",
	//	},
	//	{
	//		"scope": "orgmode.bold",
	//		"foreground": "#aaffaa",
	//		"font_style": "bold",
	//	},
	//	{
	//		"scope": "orgmode.italics",
	//		"foreground": "#aaffff",
	//		"font_style": "italic",
	//	},
	//	{
	//		"scope": "orgmode.underline",
	//		"foreground": "#aaaaff",
	//		"font_style": "underline",
	//	},
	//	{
	//		"scope": "orgmode.strikethrough",
	//		"foreground": "#aaaaaa",
	//	},
	//	{
	//		"scope": "orgmode.code",
	//		"foreground": "#ffaaff",
	//	},
	//	{
	//		"scope": "orgmode.verbatim",
	//		"foreground": "#ffaaaa",
	//	},
"""


stateBlock = """
    // GENERATED: By OrgExtended
    //
    // States are the build in state flow. While org
    // allows you to define your own state flows
    // I do not yet have a good way of automatically
    // adding those to the syntax and color scheme.
    // (I hope to one day have a way to do that)
    //
    // For now the pre-defined state flows have automatic
    // highlighting and any new ones you define will have
    // the default. I can of course extend the syntax if desired.
"""

priorityBlock = """
    // GENERATED: By OrgExtended
    //
    // Much like states I do not have a way to extend the syntax with
    // new priorities at this time. I hope to devise a good scheme in
    // the future. 
    //
    // That said there is a default set of priorities A,B,C,D,E that
    // have automatic coloring. These are the color scheme elements that
    // add that coloring.
"""

fenceBlock = """
    // GENERATED: By OrgExtended
    //
    // Code blocks have a heading BEGIN_SRC and an ending END_SRC
    // I find it visually appealing to make these stand out.
    // You may have different preferences. NOTE: I use a luminance
    // shift expression to make the chosen color work with your color scheme.
"""

datePickerBlock = """
	// GENERATED: By OrgExtended
	// ====== DATE PICKER =====
	//
	// The date picker is the calendar view widget for selecting dates
	// this view has its own color scheme. The defaults are reasonable
	// with most color schemes. You may however want to tweak one of these.
	//
	//	{
	//		"scope": "orgdatepicker.weekendheader",
	//		"foreground": "#5b96f5",
	//		"font_style": "bold italic",
	//	},
	//	{
	//		"scope": "orgdatepicker.weekdayheader",
	//		"foreground": "#0762a3",
	//		"font_style": "bold italic",
	//	},
	//	{
	//		"scope": "orgdatepicker.monthheader",
	//		"foreground": "#7e4794",
	//		"font_style": "bold italic",
	//	},
	//	{
	//		"scope": "orgdatepicker.time",
	//		"foreground": "#aaaaaa",
	//		"font_style": "bold italic",
	//	},
"""

class OrgRegenSyntaxTemplateCommand(sublime_plugin.TextCommand):
    def run(self, edit):
    	templateFile = os.path.join(sublime.packages_path(),"OrgExtended","OrgExtended.sublime-syntax-template")
    	outputFile = os.path.join(sublime.packages_path(),"OrgExtended","OrgExtended.sublime-syntax")
    	languageList = os.path.join(sublime.packages_path(),"OrgExtended","languagelist.yaml")
    	templates = ""
    	with open(languageList) as file:
    		documents = yaml.full_load(file)
    		for item in documents:
    			if 'text' in item:
    				item['source'] = "text." + item['language']
    			elif not 'source' in item:
    				item['source'] = "source." + item['language']
    			else:
    				item['source'] = "source." + item['source']
    			if not 'match' in item:
    				item['match'] = item['language']
    			templates += template.format(**item)
    	templates += "\n"
    	with open(templateFile) as tfile:
    		with open(outputFile, 'w') as ofile:
    			for line in tfile.readlines():
    				if("{{INSERT_LANGUAGES_HERE}}" in line):
    					ofile.write(templates)
    				else:
    					ofile.write(line)
    		#print(templates)

def findscope(cs, name):
	if(name == None):
		return None
	for i in cs['rules']:
		if 'scope' in i and i['scope'] == name:
			return i
	return None

def replaceVar(cs,val):
	m = varre.match(val)
	while(m):
		name = m.group('name')
		data = cs['variables'][name]
		val = varre.sub(val,data)	
		m = varre.match(val)
	return val

def expandColor(cs, val):
	return replaceVar(cs,val)

def getBackground(cs, scope = None):
	i = findscope(cs, scope)
	if(i):
		if('background' in i):
			return expandColor(cs, i['background'])
	bg = cs['globals']['background']
	if(not bg):
		return expandColor(cs, "#ffffff")
	return expandColor(cs, bg)

class OrgCreateColorSchemeFromActiveCommand(sublime_plugin.TextCommand):

	def addstates(self, cs):
		cs['rules'].append({"COMMENT ORGMODE STATES COMMENT HERE":""})
		self.addscope(cs,"orgmode.state.todo",      "#e6ab4c")
		self.addscope(cs,"orgmode.state.blocked",   "#FF0000")
		self.addscope(cs,"orgmode.state.done",      "#47c94f")
		self.addscope(cs,"orgmode.state.cancelled", "#bab9b8")
		self.addscope(cs,"orgmode.state.meeting",   "#dec7fc")
		self.addscope(cs,"orgmode.state.phone",     "#77ebed")
		self.addscope(cs,"orgmode.state.note",      "#d2a2e0")
		self.addscope(cs,"orgmode.state.doing",     "#9c9c17")
		self.addscope(cs,"orgmode.state.inprogress","#9c9c17")
		self.addscope(cs,"orgmode.state.next",      "#37dae6")
		self.addscope(cs,"orgmode.state.reassigned","#bab9b8")

	def addpriorities(self, cs):
		cs['rules'].append({"COMMENT ORGMODE PRIORITIES COMMENT HERE":""})
		self.addscope(cs,"orgmode.priority","#c27532")
		self.addscope(cs,"orgmode.priority.value","#f5a55f")
		self.addscope(cs,"orgmode.priority.value.a","#e05a7b")
		self.addscope(cs,"orgmode.priority.value.b","#f59a76")
		self.addscope(cs,"orgmode.priority.value.c","#fab978")
		self.addscope(cs,"orgmode.priority.value.d","#f5d976")
		self.addscope(cs,"orgmode.priority.value.e","#bcbfae")
		self.addscope(cs,"orgmode.priority.value.general","#b59eb5")

	def addagenda(self,cs):
		bg = getBackground(cs)
		weekEmpty = "color(" + bg + " l(+ 5%))"
		self.addscope(cs,"orgagenda.week.empty", weekEmpty, weekEmpty)	
		self.addscope(cs,"orgagenda.block.1","#623456","#623456")
		self.addscope(cs,"orgagenda.block.2","#007777","#007777")
		self.addscope(cs,"orgagenda.block.3","#999900","#999900")
		self.addscope(cs,"orgagenda.block.4","#007700","#007700")
		self.addscope(cs,"orgagenda.block.5","#aa5522","#aa5522")
		self.addscope(cs,"orgagenda.block.6","#f89cf4","#f89cf4")
		self.addscope(cs,"orgagenda.block.7","#0000ee","#0000ee")
		
		self.addscope(cs,"orgagenda.habit.didit","#ffffff","#007700")
		self.addscope(cs,"orgagenda.habit.scheduled","#333300","#550000")
		self.addscope(cs,"orgagenda.habit.nothing","#000066","#000066")
		

	def addfences(self, cs):
		cs['rules'].append({"COMMENT ORGMODE FENCE COMMENT HERE":""})
		bg = getBackground(cs, 'markup.raw.block')
		bg = "color(" + bg + " l(+ 6%))"
		self.addscope(cs,"orgmode.fence",None, bg,"bold")

	def addscope(self, cs, name, fg, bg=None, style=None):
		if(not findscope(cs, name)):
			scope = {"scope": name}
			if(fg):
				scope['foreground'] = fg
			if(bg):
				scope['background'] = bg
			if(style):
				scope['font_style'] = style
			cs['rules'].append(scope)	

	def addpreamble(self, cs):
		if(not findscope(cs, 'orgmode.preamble')):
			bg = cs['globals']['background']
			cs['rules'].append({"scope": "orgmode.preamble","foreground": bg, "background": bg})	

	def run(self, edit):
		self.settings = sublime.load_settings('Preferences.sublime-settings')
		self.origColorScheme = self.settings.get("color_scheme",None)
		if(self.origColorScheme):
			self.colorSchemeData = sublime.load_resource(self.origColorScheme)

			cs = ast.literal_eval(self.colorSchemeData)
			if(not cs):
				print("FAILED TO GENERATE NEW COLOR SCHEME COULD NOT PARSE SCHEME")
				return
			path = os.path.join(sublime.packages_path(),"User","OrgColorSchemes")
			if(not os.path.exists(path)):
				os.mkdir(path)

			cs['rules'].append({"COMMENT ORGMODE INTRO HERE":""})

			self.addpreamble(cs)
			self.addstates(cs)
			self.addfences(cs)
			self.addpriorities(cs)


			scheme = os.path.basename(self.origColorScheme)
			scheme = os.path.splitext(scheme)[0]
			schemeName = scheme + "_Org.sublime-color-scheme"
			outputFile = os.path.join(path, schemeName)
			cs['rules'].append({"COMMENT ORGMODE SCOPES HERE":""})
			cs['rules'].append({"COMMENT ORGMODE DATEPICKER SCOPES HERE":""})
			
			self.addagenda(cs)

			jsonStr = json.dumps(cs, sort_keys=True, indent=4)


			jsonStr = jsonStr.replace('"COMMENT ORGMODE SCOPES HERE": ""',commentBlock)
			jsonStr = jsonStr.replace('"COMMENT ORGMODE INTRO HERE": ""',introBlock)
			jsonStr = jsonStr.replace('"COMMENT ORGMODE FENCE COMMENT HERE": ""',fenceBlock)
			jsonStr = jsonStr.replace('"COMMENT ORGMODE PRIORITIES COMMENT HERE": ""',priorityBlock)
			jsonStr = jsonStr.replace('"COMMENT ORGMODE STATES COMMENT HERE": ""',stateBlock)
			jsonStr = jsonStr.replace('"COMMENT ORGMODE DATEPICKER SCOPES HERE": ""',datePickerBlock)
			with open(outputFile,'w') as ofile:
				ofile.write(jsonStr)
			newColorScheme = "Packages/User/OrgColorSchemes/" + schemeName
			print("CHANGING ORIGINAL COLOR SCHEME: " + self.origColorScheme)
			print("TO COLOR SCHEME: " + newColorScheme)
			self.mysettings = sublime.load_settings('OrgExtended.sublime-settings')
			self.mysettings.set("color_scheme", newColorScheme)
			sublime.save_settings('OrgExtended.sublime-settings')

			self.mysettings = sublime.load_settings('orgdatepicker.sublime-settings')
			self.mysettings.set("color_scheme", newColorScheme)
			sublime.save_settings('orgdatepicker.sublime-settings')

			self.mysettings = sublime.load_settings('orgagenda.sublime-settings')
			self.mysettings.set("color_scheme", newColorScheme)
			sublime.save_settings('orgagenda.sublime-settings')



