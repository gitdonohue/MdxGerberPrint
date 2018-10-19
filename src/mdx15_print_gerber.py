#
# Print a gerber file to the MDX-15, optionally setting the home position
#
# Note: Uses RawFileToPrinter.exe as found at http://www.columbia.edu/~em36/windowsrawprint.html
# Note: Might work with other Roland Modela Models (MDX-20), but I don't have access to such machines, so I cannot test.
#
#
# MIT License
#
# Copyright (c) 2018 Charles Donohue
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

import re
import os 

import msvcrt 
import sys
import serial

class GCode2RmlConverter:

	# stateful variables
	inputConversionFactor = 1.0 # mm units
	X = 0.0
	Y = 0.0
	Z = 0.0
	speedmode = None
	feedrate = 0.0
	isFirstCommand = True
	offset_x = 0.0
	offset_y = 0.0
	feedspeedfactor = 1.0

	def __init__(self,offset_x,offset_y,feedspeedfactor):
		self.moveCommandParseRegex = re.compile(r'G0([01])\s(X([-+]?\d*\.*\d+\s*))?(Y([-+]?\d*\.*\d+\s*))?(Z([-+]?\d*\.*\d+\s*))?')
		self.offset_x = offset_x
		self.offset_y = offset_y
		self.feedspeedfactor = feedspeedfactor

	def digestStream(self, lineIterator):
		outputCommands = []
		for line in lineIterator :
			outputCommands.extend( self.digestLine(line) )
		return outputCommands

	def digestLine(self,line):
		outputCommands = []

		if self.isFirstCommand :
			self.isFirstCommand = False
			# Initialization commands
			outputCommands.append('^DF') # set to defaults
			#outputCommands.append('! 1;Z 0,0,813') # not sure what this does. Maybe starts the spindle? TODO: Try without.

		line = line.rstrip() # strip line endings
		#print('cmd: '+line)
		if line == None or len(line) == 0 :
			pass # empty line
		elif line.startswith('(') :
			pass # comment line
		elif line == 'G20' : # units as inches
			self.inputConversionFactor = 25.4
		elif line == 'G21' : # units as mm
			self.inputConversionFactor = 1.0
		elif line == 'G90' : # absolute mode
			pass # implied
		elif line == 'G94' : # Feed rate units per minute mode
			pass # implied
		elif line == 'M03' : # spindle on
			pass
		elif line == 'M05' : # spindle off
			outputCommands.append('^DF;!MC0;')
			outputCommands.append('H')
		elif line.startswith('G01 F'): # in flatcam 2018, the feed rate is set in a move command
			self.feedrate = float(line[5:]) 
		elif line.startswith('G00') or line.startswith('G01'): # move
			outputCommands.extend( self.processMoveCommand(line) )	
		elif line.startswith('G4 P'): # dwell
			dwelltime = int(line[4:])
			outputCommands.append('W {}'.format( dwelltime ) )
		elif line.startswith('F'): # feed rate
			self.feedrate = float(line[1:]) 
		# ...
		else :
			print('Unrecognized command: ' + line)
			pass
		return outputCommands

	def processMoveCommand(self, line):
		#print(line)
		outputCommands = []
		g = self.moveCommandParseRegex.match(line)
		if self.speedmode != g.group(1) :
			self.speedmode = g.group(1)
			#print( 'speed changed: ' + self.speedmode )
			f = self.feedrate * self.inputConversionFactor * self.feedspeedfactor / 60.0 # convert to mm per second
			if self.speedmode == '0' : f = 16.0 # fast mode
			outputCommands.append('V {0:.2f};F {0:.2f}'.format(f)) 
		if g.group(3) != None : self.X = float(g.group(3)) * self.inputConversionFactor
		if g.group(5) != None : self.Y = float(g.group(5)) * self.inputConversionFactor
		if g.group(7) != None : self.Z = float(g.group(7)) * self.inputConversionFactor
		#outputScale = 1 / 0.01
		outputScale = 1 / 0.025
		outputCommands.append('Z {:.3f},{:.3f},{:.3f}'.format(self.X*outputScale+self.offset_x,self.Y*outputScale+self.offset_y,self.Z*outputScale))
		return outputCommands

	def convertFile(self,infile,outfile):
		# TODO: Handle XY offsets
		inputdata = open(infile)
		outdata = self.digestStream(inputdata)
		outfile = open(outfile,'w')
		for cmd in outdata :
			outfile.write(cmd)
			outfile.write('\n')
			#print(cmd)

##################################################


class ModelaZeroControl:
	# Constants
	XY_INCREMENTS = 1
	XY_INCREMENTS_LARGE= 100
	Z_INCREMENTS = 1
	Z_INCREMENTS_MED = 10
	Z_INCREMENTS_LARGE = 100
	Z_DEFAULT_OFFSET = -1300.0

	Y_MAX = 4064.0
	X_MAX = 6096.0

	comport = None
	ser = None

	z_offset = 0.0
	x = 0.0
	y = 0.0
	z = 0.0

	connected = False

	def __init__(self,comport):
		self.comport = comport
		try :
			self.ser = serial.Serial(self.comport,9600,rtscts=1)
			self.ser.close()
			self.ser = None
			self.connected = True
		except serial.serialutil.SerialException as e :
			print('Could not open '+comport)
			self.connected = False
			#sys.exit(1)

	def sendCommand(self,cmd):
		#print(cmd)
		try :
			self.ser = serial.Serial(self.comport,9600,rtscts=1)
			txt = cmd + '\n'
			self.ser.write(txt.encode('ascii'))
			self.ser.close()
			self.ser = None
		except serial.serialutil.SerialException as e :
			#print(e)
			print('Error writing to '+self.comport)
			self.connected = False
			#sys.exit(1)

	def sendMoveCommand(self):
		if self.x < 0.0 : self.x = 0.0
		if self.x > self.X_MAX : self.x = self.X_MAX 
		if self.y < 0.0 : self.y = 0.0
		if self.y > self.Y_MAX : self.y = self.Y_MAX
		print('Moving to {:.3f},{:.3f},{:.3f}'.format(self.x,self.y,self.z))
		spindle = '1' if self.spindleEnabled else '0'
		# The esoteric syntax was borrowed from https://github.com/Craftweeks/MDX-LabPanel
		self.sendCommand('^DF;!MC{0};!PZ0,0;V15.0;Z{1:.3f},{2:.3f},{3:.3f};!MC{0};;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;'.format(spindle,self.x,self.y,self.z))

	def run(self):

		print('If the green light next to the VIEW button is lit, please press the VIEW button.')
		print('Usage:')
		print('\th - send to home')
		print('\tz - send to zero')	
		print('\twasd - move on the XY plane (+shift for small increments)')
		print('\tup/down - move in the Z axis (+CTRL for medium increments, +ALT for large increments)')
		print('\tZ - Set Z zero')
		print('\tq/ESC - Quit')

		self.sendCommand('^IN;!MC0;H') # clear errors, disable spindle, return home
		self.z_offset = self.Z_DEFAULT_OFFSET
		self.sendCommand('^DF;!ZO{:.3f};;'.format(self.z_offset)) # set z zero half way
		self.x = 0.0
		self.y = 0.0
		self.z = 0.0
		self.spindleEnabled = False
		self.sendMoveCommand()

		self.xy_zero = (0.0,0.0)
		
		while self.connected :
			c = msvcrt.getwch()
			n = 0
			if c == '\xe0' or c == '\x00' :
				c = msvcrt.getwche()
				n = ord(c)
				#print(c,n)

			if ( c == 'q' and n == 0 ) or ord(c) == 3 or ord(c) == 27 :
				print('Done') 
				return self.xy_zero

			elif c == 'h' :
				 self.sendCommand('^DF;!MC0;H')

			elif c == 'z' :
				 #self.x = 0.0
				 #self.y = 0.0
				 (self.x,self.y) = self.xy_zero
				 self.z = 0.0
				 self.sendMoveCommand()

			elif c == 'w' and n == 0 :
				self.y += self.XY_INCREMENTS_LARGE
				self.sendMoveCommand()
			elif c == 's' and n == 0 :
				self.y -= self.XY_INCREMENTS_LARGE
				self.sendMoveCommand()
			elif c == 'd' and n == 0 :
				self.x += self.XY_INCREMENTS_LARGE
				self.sendMoveCommand()
			elif c == 'a' and n == 0 :
				self.x -= self.XY_INCREMENTS_LARGE
				self.sendMoveCommand()

			elif c == 'W' and n == 0 :
				self.y += self.XY_INCREMENTS
				self.sendMoveCommand()
			elif c == 'S' and n == 0 :
				self.y -= self.XY_INCREMENTS
				self.sendMoveCommand()
			elif c == 'D' and n == 0 :
				self.x += self.XY_INCREMENTS
				self.sendMoveCommand()
			elif c == 'A' and n == 0 :
				self.x -= self.XY_INCREMENTS
				self.sendMoveCommand()

			elif n == 72 : # up arrow
				self.z += self.Z_INCREMENTS
				self.sendMoveCommand()
			elif n == 80 : # down arrow
				self.z -= self.Z_INCREMENTS
				self.sendMoveCommand()
			elif n == 141 : # ctrl + up arrow
				self.z += self.Z_INCREMENTS_MED
				self.sendMoveCommand()
			elif n == 145 : # ctrl + down arrow
				self.z -= self.Z_INCREMENTS_MED
				self.sendMoveCommand()
			elif n == 152 : # alt + up arrow
				self.z += self.Z_INCREMENTS_LARGE
				self.sendMoveCommand()
			elif n == 160 : # alt + down arrow
				self.z -= self.Z_INCREMENTS_LARGE
				self.sendMoveCommand()

			elif c == 'Z' and n == 0 :
				# Set Z zero to current position
				print('Setting zero')
				self.z_offset = self.z_offset + self.z
				self.z = 0.0
				self.sendCommand('^DF;!ZO{:.3f};;'.format(self.z_offset)) # set z zero to current
				self.xy_zero = (self.x,self.y)

			elif n == 75 : # left arrow
				#self.sendCommand('^DF;!MC0;') # disable spindle
				self.spindleEnabled = False
				self.sendMoveCommand()
			elif n == 77 : # right arrow
				#self.sendCommand('^DF;!MC1;') # enable spindle
				self.spindleEnabled = True
				self.sendMoveCommand()

			else :
				#print( 'you entered: ' + str(n if n != 0 else ord(c) ))
				pass

		return (0.0,0.0)

	def setZeroHere(self) :
		print('Setting zero')
		self.z_offset = self.z_offset + self.z
		self.z = 0.0
		self.sendCommand('^DF;!ZO{:.3f};;'.format(self.z_offset)) # set z zero to current
		self.xy_zero = (self.x,self.y)
		return self.xy_zero

##################################################

def main():
	
	import optparse	
	parser = optparse.OptionParser('usage%prog -i <input file>')
	parser.add_option('-i', '--infile', dest='infile', default='', help='The input gcode file, as exported by FlatCam.')
	parser.add_option('-o', '--outfile', dest='outfile', default='', help='The output RML-1 file.')
	parser.add_option("-z", '--zero', dest='zero', action="store_true", default=False, help='Zero the print head on the work surface.')
	#parser.add_option('-s', '--serialport', dest='serialport', default='', help='The com port for the MDX-15. (Default: obtained from the printer driver)')
	parser.add_option("-p", '--print', dest='print', action="store_true", default=False, help='Prints the RML-1 data.')
	parser.add_option('-n', '--printerName', dest='printerName', default='Roland MODELA MDX-15', help='The windows printer name. (Default: Roland MODELA MDX-15)')
	parser.add_option('-f', '--feedspeedfactor', dest='feedspeedfactor', default=1.0, help='Feed rate scaling factor (Default: 1.0)')
	(options,args) = parser.parse_args()
	#print(options)

	serialport = ''
	import subprocess
	shelloutput = subprocess.check_output('powershell -Command "(Get-WmiObject Win32_Printer -Filter \\"Name=\'{}\'\\").PortName"'.format(options.printerName))
	if len(shelloutput)>0 :
		try :
			serialport = shelloutput.decode('utf-8').split(':')[0]
			print( 'Found {} on {}'.format(options.printerName,serialport) )
		except:
			print('Error parsing com port: ' + str(shelloutput) )
	else :
		print('Could not find the printer driver for: ' + options.printerName)
		sys.exit(1)

	x_offset = 0.0
	y_offset = 0.0
	if options.zero :
		print('Setting Zero')
		modelaZeroControl = ModelaZeroControl(serialport)
		(x_offset,y_offset) = modelaZeroControl.run()

		print('Would you like to set the current position as the Zero (y/n)?')
		c = msvcrt.getwch()
		if c == 'y' or c == 'Y' :
			(x_offset,y_offset) = modelaZeroControl.setZeroHere()


	if options.infile != '' :
		if options.outfile == '' : options.outfile = options.infile + '.prn'
		print('Converting {} to {}'.format(options.infile,options.outfile))
		converter = GCode2RmlConverter(x_offset, y_offset, float(options.feedspeedfactor))
		converter.convertFile( options.infile, options.outfile )

	if options.print :
		if options.outfile != '' :
			print('Printing: '+options.outfile)
			os.system('RawFileToPrinter.exe "{}" "{}"'.format(options.outfile,options.printerName)) 

			print('Procedure to cancel printing:')
			print('1) Press the VIEW button on the printer.')
			print('2) Cancel the print job(s) in windows. (start->Devices and Printers->...)')
			print('3) Remove the usb cable to the printer.')
			print('4) Press both the UP and Down buttons on the printer.')
			print('5) When VIEW light stops blinking, press the VIEW butotn.')
			print('6) Plug the usb cable back in.')
		else :
			print('Error: No file to be printed.')


if __name__ == "__main__":
	if sys.version_info[0] < 3 :
		print("This script requires Python version 3")
		sys.exit(1)
	main()