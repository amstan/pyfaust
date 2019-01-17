#!/usr/bin/env python3

import cffi
import collections
#import jack
import os
import re
from subprocess import Popen, PIPE

_faust_data_structures = open("/usr/share/faust/pure.c").read()
_faust_data_structures = _faust_data_structures.replace("<<includeIntrinsic>>", "")
_faust_data_structures = _faust_data_structures.replace("<<includeclass>>", "")

class FaustCompilerError(Exception):
	pass

ui_elements = {}
def ui_element(cls):
	field_name = "add%s" % cls.__name__
	signature = "%sFun" % field_name
	def wrap(dsp_instance):
		def instanciate_element_at_callback(_interface, label, *args):
			label = dsp_instance._ffi.string(label).decode("utf8")
			element_instance = cls(label, *args)
			if hasattr(element_instance, "__get__"):
				# put it on the class, so it shows up as a property
				setattr(dsp_instance.ui.__class__, label, element_instance)
			else:
				# just throw it directly, so it won't turn into magic
				setattr(dsp_instance.ui, label, element_instance)
			dsp_instance.ui.elements[label] = element_instance
			element_instance.declarations = {}
			print(repr(element_instance))
		callback = dsp_instance._ffi.callback(signature, instanciate_element_at_callback)
		dsp_instance.ui._callbacks.add(callback) #keep a reference or else we get segfault
		return callback
	ui_elements[field_name] = wrap
	return wrap

class BooleanElement(object):
	def __init__(self, label, zone):
		self.label = label
		self.zone = zone
	@property
	def value(self):
		return bool(self.zone[0])
	@value.setter
	def value(self, value):
		self.zone[0] = bool(value)

@ui_element
class Button(BooleanElement):
	def click(self, ms=100):
		import time
		self.zone[0] = 1
		time.sleep(ms / 1000.0)
		self.zone[0] = 0
	__call__ = click
	def __repr__(self):
		return "((%s))" % (self.label)

@ui_element
class CheckButton(BooleanElement):
	def __repr__(self):
		return "[%c] %s" % (" X"[int(self.zone[0])], self.label)

class Slider(object):
	def __init__(self, label, zone, init, min, max, step):
		self.label = label
		self.zone = zone
		self.init = init
		self.min = min
		self.max = max
		self.step = step
	@property
	def value(self):
		return self.zone[0]
	@value.setter
	def value(self, value):
		#clamp
		value = max(value, self.min)
		value = min(value, self.max)
		#TODO: step validation
		self.zone[0] = value
	def __get__(self, _, __):
		return self.value
	def __set__(self, _, value):
		self.value = value

@ui_element
class HorizontalSlider(Slider):
	def __repr__(self):
		return "%s: %f---<%f>---%f" % (self.label, self.min, self.zone[0], self.max)

@ui_element
class VerticalSlider(Slider):
	def __repr__(self):
		return "%s: %f---v%f^---%f" % (self.label, self.min, self.zone[0], self.max)

@ui_element
class NumEntry(Slider):
	def __repr__(self):
		current = self.zone[0]
		def frange():
			x = self.min
			while x < self.max:
				if (abs(x - current) > self.step / 2):
					yield str(x)
				else:
					yield "**%s**" % x
				x += self.step
		return "%s: [%s]" % (self.label, ', '.join(frange()))

class FaustDSP(object):
	FAUSTFLOAT = "double"
	metadata = {}
	declarations = {}

	def _transpile(self):
		dsp_src = bytes(self.dsp_src, encoding="utf8")
		with Popen(["faust", "-lang", "c", "/dev/stdin", "-cn", "DSP"], stdin=PIPE, stdout=PIPE, stderr=PIPE) as faust_compiler:
			c_src, stderr = faust_compiler.communicate(input=dsp_src)

		stderr = stderr.decode("utf8")
		if stderr:
			raise FaustCompilerError(stderr)

		self.c_src = c_src.decode("utf8")

	def _cleanup_cpp(self, src):
		sections = [
			"#define FAUSTFLOAT (.*)",
			"#include .*",
			"#define .*",
			"#ifdef .*",
			"#ifndef .*",
			"#endif.*",
		]

		for section in sections:
			section = '\\s'.join(section.split())
			src = re.sub(section, "", src, flags=re.MULTILINE)

		src = src.replace("FAUSTFLOAT", self.FAUSTFLOAT)

		return src

	def _init_metadata(self):
		@self._ffi.callback("metaDeclareFun")
		def metaDeclare(_handle, key, value):
			key = self._ffi.string(key).decode("utf8")
			value = self._ffi.string(value).decode("utf8")
			self.metadata[key] = value

		metaglue = self._ffi.new("MetaGlue[1]")
		metaglue[0].declare = metaDeclare
		self._lib.metadataDSP(metaglue)

	def _init_ui(self):
		# Prepare a class for the ui elements.
		# We want the class local because we're going to have different
		# properties depending on what we're told.
		class UI(object):
			_callbacks = set()
			elements = {}
			declarations = collections.defaultdict(dict)
			def __repr__(self):
				return "\n".join(repr(e) + "\t" + repr(e.declarations) for e in self.elements.values())
		self.ui = UI()

		@self._ffi.callback("openTabBoxFun")
		def openTabBox(_interface, label):
			label = self._ffi.string(label).decode("utf8")
			print("/ %s \\ {" % label)

		@self._ffi.callback("openVerticalBoxFun")
		def openVerticalBox(_interface, label):
			label = self._ffi.string(label).decode("utf8")
			print("| %s | {" % label)

		@self._ffi.callback("openHorizontalBoxFun")
		def openVerticalBox(_interface, label):
			label = self._ffi.string(label).decode("utf8")
			print("- %s - {" % label)

		@self._ffi.callback("closeBoxFun")
		def closeBox(_interface):
			print("}")

		@self._ffi.callback("declareFun")
		def declare(_interface, zone, key, value):
			# append declarations for now, we'll assign them later
			key = self._ffi.string(key).decode("utf8")
			value = self._ffi.string(value).decode("utf8")
			self.ui.declarations[zone][key] = value

		# Setup the uiglue struct and call the builder
		uiglue = self._ffi.new("UIGlue[1]")
		for attr_name, element in ui_elements.items():
			setattr(uiglue[0], attr_name, element(self))
		uiglue[0].openVerticalBox = openVerticalBox
		uiglue[0].openHorizontalBox = openVerticalBox
		uiglue[0].declare = declare
		uiglue[0].closeBox = closeBox
		self._lib.buildUserInterfaceDSP(self._struct, uiglue)

		# assign declarations
		for zone, per_zone_declarations in tuple(self.ui.declarations.items()):
			for element in self.ui.elements.values():
				if element.zone == zone:
					element.declarations = per_zone_declarations
					for key, value in per_zone_declarations.items():
							setattr(element, key, value)
					del self.ui.declarations[zone]
					self.ui.declarations[element] = per_zone_declarations

	def __init__(self, dsp_src, name="mydsp"):
		self.dsp_src = dsp_src
		self.name = name

		self._transpile()

		self.c_src = "#define FAUSTFLOAT " + self.FAUSTFLOAT + "\n" + self.c_src
		self.c_src = _faust_data_structures + self.c_src

		with open("faustffi_" + self.name + ".src.c", "w") as f:
			f.write(self.c_src)

		self._ffi = cffi.FFI()
		self._ffi.set_source("faustffi_" + self.name, self.c_src)
		self._ffi.cdef(self._cleanup_cpp(_faust_data_structures))
		self._ffi.cdef("""
			typedef ... DSP;
			DSP* newDSP();
			void deleteDSP(DSP* dsp);
			void metadataDSP(MetaGlue* m);
			int getSampleRateDSP(DSP* dsp);
			int getNumInputsDSP(DSP* dsp);
			int getNumOutputsDSP(DSP* dsp);
			int getInputRateDSP(DSP* dsp, int channel);
			int getOutputRateDSP(DSP* dsp, int channel);
			void classInitDSP(int samplingFreq);
			void instanceResetUserInterfaceDSP(DSP* dsp);
			void instanceClearDSP(DSP* dsp);
			void instanceConstantsDSP(DSP* dsp, int samplingFreq);
			void instanceInitDSP(DSP* dsp, int samplingFreq);
			void initDSP(DSP* dsp, int samplingFreq);
			void buildUserInterfaceDSP(DSP* dsp, UIGlue* ui_interface);
			void computeDSP(DSP* dsp, int count, FAUSTFLOAT** inputs, FAUSTFLOAT** outputs);
		""".replace("FAUSTFLOAT", self.FAUSTFLOAT))

		self._lib = self._ffi.dlopen(self._ffi.compile())

		self._struct = self._ffi.gc(self._lib.newDSP(), self._lib.deleteDSP)
		self._init_metadata()
		self._lib.initDSP(self._struct, 48000)
		self._init_ui()

		self.num_inputs = self._lib.getNumInputsDSP(self._struct)
		self.num_outputs = self._lib.getNumOutputsDSP(self._struct)

	def init_buffers(self, samples):
		self.samples = samples
		self.input_buffers = [
			self._ffi.new("%s[]" % self.FAUSTFLOAT, self.samples)
				for i in range(self.num_inputs)
		]
		self.output_buffers = [
			self._ffi.new("%s[]" % self.FAUSTFLOAT, self.samples)
				for i in range(self.num_outputs)
		]

	def compute(self):
		self._lib.computeDSP(self._struct, self.samples, self.input_buffers, self.output_buffers)
		#return list(self.output_buffers[0])

	@property
	def samplerate(self):
		return self._lib.getSampleRateDSP(self._struct)

if __name__=="__main__":
	import pprint

	src = """
		import("all.lib");
		process = pm.guitar_ui_MIDI;
	"""
	self = FaustDSP(src, name="Main")
	self.init_buffers(1024)

	def test_sound():
		import soundcard
		import threading
		default_speaker = soundcard.default_speaker()
		def worker():
			with default_speaker.player(samplerate=self.samplerate, blocksize=self.samples) as player:
				while True:
					self.compute()
					player.play(list(self.output_buffers[0]))
		t = threading.Thread(target=worker)
		t.daemon = True
		t.start()
	test_sound()
