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
import time
import sys
import threading
import traceback
import math

import msvcrt 
import serial
import cv2
import numpy

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

	# Backlash compensation related
	backlashX = 0
	backlashY = 0
	backlashZ = 0
	last_x = 0
	last_y = 0
	last_z = 0
	last_displacement_x = 0.0
	last_displacement_y = 0.0
	last_displacement_z = 0.0
	backlash_compensation_x = 0.0
	backlash_compensation_y = 0.0
	backlash_compensation_z = 0.0
	epsilon = 0.001

	levelingData = None
	manualLevelingPoints = None

	def __init__(self,offset_x,offset_y,feedspeedfactor,backlashX,backlashY,backlashZ,levelingData,manualLevelingPoints):
		self.moveCommandParseRegex = re.compile(r'G0([01])\s(X([-+]?\d*\.*\d+\s*))?(Y([-+]?\d*\.*\d+\s*))?(Z([-+]?\d*\.*\d+\s*))?')
		self.offset_x = offset_x
		self.offset_y = offset_y
		self.feedspeedfactor = feedspeedfactor
		self.backlashX = backlashX
		self.backlashY = backlashY
		self.backlashZ = backlashZ
		self.levelingData = levelingData
		self.manualLevelingPoints = manualLevelingPoints

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

	def getHeightFor3PointPlane( self, p1,p2,p3, x, y ):
		x1, y1, z1 = p1
		x2, y2, z2 = p2
		x3, y3, z3 = p3
		v1 = [x3 - x1, y3 - y1, z3 - z1]
		v2 = [x2 - x1, y2 - y1, z2 - z1]
		cp = [v1[1] * v2[2] - v1[2] * v2[1],  v1[2] * v2[0] - v1[0] * v2[2],  v1[0] * v2[1] - v1[1] * v2[0]]
		a, b, c = cp
		d = a * x1 + b * y1 + c * z1
		z = (d - a * x - b * y) / float(c)
		return z

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

		# Z height correction
		z_correction = 0.0
		if self.levelingData != None :
			n = len( self.levelingData[0] )
			px = self.X*outputScale #+self.offset_x
			py = self.Y*outputScale #+self.offset_y

			# Find quadrant in which point lies
			i = 0
			j = 0
			while i < (n-2) :
				if px >= (self.levelingData[i][j][0]-self.epsilon) and px < self.levelingData[i+1][j][0] : break
				i = i+1
			while j < (n-2) :
				if py >= (self.levelingData[i][j][1]-self.epsilon) and py < self.levelingData[i][j+1][1] : break
				j = j+1

			# interpolate values
			
			px0 = self.levelingData[i][j][0]
			px1 = self.levelingData[i+1][j][0]
			fx = (px - px0) / (px1 - px0)
			h00 = self.levelingData[i][j][2]
			h10 = self.levelingData[i+1][j][2]
			h0 = h00 + (h10 - h00) * fx
			h01 = self.levelingData[i][j+1][2]
			h11 = self.levelingData[i+1][j+1][2]
			h1 = h01 + (h11 - h01) * fx
			py0 = self.levelingData[i][j][1]
			py1 = self.levelingData[i][j+1][1]
			fy = (py - py0) / (py1 - py0)
			h = h0 + (h1 - h0) * fy
			#print(px,py,i,j,fx,fy,self.Z,h,h/outputScale)
			z_correction = -h
			# Apply compensation to Z
			#self.Z = self.Z - h/outputScale

		# Manual leveling points	
		elif self.manualLevelingPoints != None :
			if len(self.manualLevelingPoints) < 3 :
				pass # At least 3 points required
			else :
				px = self.X*outputScale #+self.offset_x
				py = self.Y*outputScale #+self.offset_y
				h = self.getHeightFor3PointPlane( self.manualLevelingPoints[0], self.manualLevelingPoints[1], self.manualLevelingPoints[2], px, py )
				z_correction = +h
				pass

		# Backlash handling in X
		if abs(self.backlashX) > self.epsilon :
			deltaX = self.X - self.last_x 
			if abs(deltaX) > self.epsilon : # non-zero move in that axis
				if deltaX * self.last_displacement_x < 0 : # direction changed
					# move to last position with offset in new move dir
					self.backlash_compensation_x = 0.0 if deltaX > 0 else -self.backlashX
					outputCommands.append('Z {:.0f},{:.0f},{:.0f}'.format(self.last_x*outputScale+self.offset_x+self.backlash_compensation_x,self.last_y*outputScale+self.offset_y+self.backlash_compensation_y,self.last_z*outputScale+self.backlash_compensation_z+z_correction))
				self.last_displacement_x = deltaX;

		# Backlash handling in Y
		if abs(self.backlashY) > self.epsilon :
			deltaY = self.Y - self.last_y 
			if abs(deltaY) > self.epsilon : # non-zero move in that axis
				if deltaY * self.last_displacement_y < 0 : # direction changed
					# move to last position with offset in new move dir
					self.backlash_compensation_y = 0.0 if deltaY > 0 else -self.backlashY
					outputCommands.append('Z {:.0f},{:.0f},{:.0f}'.format(self.last_x*outputScale+self.offset_x+self.backlash_compensation_x,self.last_y*outputScale+self.offset_y+self.backlash_compensation_y,self.last_z*outputScale+self.backlash_compensation_z+z_correction))
				self.last_displacement_y = deltaY;

		# Backlash handling in Z
		if abs(self.backlashZ) > self.epsilon :
			deltaZ = self.Z - self.last_z 
			if abs(deltaZ) > self.epsilon : # non-zero move in that axis
				if deltaZ * self.last_displacement_z < 0 : # direction changed
					# move to last position with offset in new move dir
					self.backlash_compensation_z = 0.0 if deltaZ > 0 else -self.backlashZ
					outputCommands.append('Z {:.0f},{:.0f},{:.0f}'.format(self.last_x*outputScale+self.offset_x+self.backlash_compensation_x,self.last_y*outputScale+self.offset_y+self.backlash_compensation_y,self.last_z*outputScale+self.backlash_compensation_+z_correction))
				self.last_displacement_z = deltaZ;

		self.last_x = self.X		
		self.last_y = self.Y
		self.last_z = self.Z

		# Send move command
		outputCommands.append('Z {:.0f},{:.0f},{:.0f}'.format(self.X*outputScale+self.offset_x+self.backlash_compensation_x, self.Y*outputScale+self.offset_y+self.backlash_compensation_y, self.Z*outputScale+self.backlash_compensation_z+z_correction))
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
	FAST_TRAVEL_RATE = 600.0

	Y_MAX = 4064.0
	X_MAX = 6096.0

	comport = None
	ser = None

	z_offset = 0.0
	x = 0.0
	y = 0.0
	z = 0.0
	last_x = 0.0
	last_y = 0.0
	last_z = 0.0

	microscope_leveling_startpoint = None
	microscope_leveling_endpoint = None

	connected = False
	hasZeroBeenSet = False
	exitRequested = False

	xy_zero = (0.0,0.0)
	manual_leveling_points = None

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

	def sendMoveCommand(self,wait=False):
		if self.x < 0.0 : self.x = 0.0
		if self.x > self.X_MAX : self.x = self.X_MAX 
		if self.y < 0.0 : self.y = 0.0
		if self.y > self.Y_MAX : self.y = self.Y_MAX
		#print('Moving to {:.0f},{:.0f},{:.0f}'.format(self.x,self.y,self.z))

		spindle = '1' if self.spindleEnabled else '0'
		# The esoteric syntax was borrowed from https://github.com/Craftweeks/MDX-LabPanel
		self.sendCommand('^DF;!MC{0};!PZ0,0;V15.0;Z{1:.3f},{2:.3f},{3:.3f};!MC{0};;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;'.format(spindle,self.x,self.y,self.z))

		# Optional wait for move complete
		dx = self.x - self.last_x
		self.last_x = self.x
		dy = self.y - self.last_y
		self.last_y = self.y
		dz = self.z - self.last_z
		self.last_z = self.z
		traveldist = math.sqrt(dx*dx+dy*dy+dz*dz)
		if wait :
			travelTime = traveldist / self.FAST_TRAVEL_RATE 
			time.sleep(travelTime)
			#print('move done')

	def run(self):

		print('If the green light next to the VIEW button is lit, please press the VIEW button.')
		print('Usage:')
		print('\th - send to home')
		print('\tz - Set Z zero')		
		print('\tZ - send to zero')	
		print('\twasd - move on the XY plane (+shift for small increments)')
		print('\tup/down - move in the Z axis (+CTRL for medium increments, +ALT for large increments)')
		print('\t1 - Set Microscope-based levelling starting point (both points must be set for autolevelling to happen)')
		print('\t2 - Set Microscope-based levelling ending point')
		print('\tm - Add manual levelling ending point (wrt zero, which must be set)')
		print('\tq - Quit and move to next step.')
		print('\tCTRL-C / ESC - Exit program.')

		self.sendCommand('^IN;!MC0;H') # clear errors, disable spindle, return home
		self.z_offset = self.Z_DEFAULT_OFFSET
		self.sendCommand('^DF;!ZO{:.3f};;'.format(self.z_offset)) # set z zero half way
		self.x = 0.0
		self.y = 0.0
		self.z = 0.0
		self.spindleEnabled = False
		self.sendMoveCommand(True)

		self.xy_zero = (0.0,0.0)
		
		while True : #self.connected :
			c = msvcrt.getwche()
			n = 0
			#print(c)
			if c == '\xe0' or c == '\x00' :
				c = msvcrt.getwche()
				n = ord(c)
				#print(c,n)

			if ( c == 'q' and n == 0 ) :
				if not self.hasZeroBeenSet :
					print('Would you like to set the current position as the Zero (y/n)?')
					c = msvcrt.getwch()
					if c == 'y' or c == 'Y' :
						self.setZeroHere()
				print('Done') 
				return self.xy_zero

			elif c == 'h' :
				 self.sendCommand('^DF;!MC0;H')

			elif c == 'Z' :
				 (self.x,self.y) = self.xy_zero
				 self.z = 0.0
				 self.sendMoveCommand(True)

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

			elif c == 'z' and n == 0 :
				self.setZeroHere()
			elif c == 'm' and n == 0 :
				self.setLevelingPointHere()

			elif n == 75 : # left arrow
				#self.sendCommand('^DF;!MC0;') # disable spindle
				self.spindleEnabled = False
				self.sendMoveCommand()
			elif n == 77 : # right arrow
				#self.sendCommand('^DF;!MC1;') # enable spindle
				self.spindleEnabled = True
				self.sendMoveCommand()

			elif c == '1' :
				self.microscope_leveling_startpoint = (self.x,self.y,self.z)
				print('Setting leveling point 1 ({:.3f},{:.3f},{:.3f})'.format(self.x,self.y,self.z))
			elif c == '2' :
				self.microscope_leveling_endpoint = (self.x,self.y,self.z)
				print('Setting leveling point 2 ({:.3f},{:.3f},{:.3f})'.format(self.x,self.y,self.z))

			elif ord(c) == 27 : # Esc
				self.exitRequested = True
				return self.xy_zero
			elif ord(c) == 3 : # CTRL-C
				self.exitRequested = True
				return self.xy_zero
			else :
				print( 'you entered: ' + str(n if n != 0 else ord(c) ))
				pass

		return self.xy_zero

	def setZeroHere(self) :
		print('Setting zero')
		self.z_offset = self.z_offset + self.z
		self.z = 0.0
		self.sendCommand('^DF;!ZO{:.3f};;'.format(self.z_offset)) # set z zero to current
		self.xy_zero = (self.x,self.y)
		self.hasZeroBeenSet = True
		if self.manual_leveling_points != None :
			print('Warning: previously set manual leveling points lost.')
		self.manual_leveling_points = None
		return self.xy_zero

	def setLevelingPointHere(self):
		if not self.hasZeroBeenSet :
			print('Warning: zero must be set before setting the leveling point. Setting it here.')
			self.setZeroHere()
		else :
			if self.manual_leveling_points == None:
				self.manual_leveling_points = [ (self.xy_zero[0],self.xy_zero[1],0.0) ]
			print('Adding leveling point {} ({:.3f},{:.3f},{:.3f})'.format(len(self.manual_leveling_points),self.x,self.y,self.z))
			self.manual_leveling_points.append( (self.x,self.y,self.z) )

	def getManualLevelingPoints(self):
		return self.manual_leveling_points

	def moveTo(self,x,y,z,wait=False):
		self.x = x
		self.y = y
		self.z = z
		self.sendMoveCommand(wait)

	def getAutolevelingData(self, cam, steps=1, heightpoints=50) :
		if self.microscope_leveling_startpoint != None and  self.microscope_leveling_endpoint != None :
			print(self.microscope_leveling_startpoint,self.microscope_leveling_endpoint)
			(x1,y1,z1) = self.microscope_leveling_startpoint
			(x2,y2,z2) = self.microscope_leveling_endpoint

			startingHeight = z1 + heightpoints/2

			self.moveTo(x1,y1,z1,wait=True) # Go to start
			
			#print(p1,p2)
			heights = [[(0,0,0) for i in range(steps+1)] for j in range(steps+1)]

			for i in range(steps+1) :
				for j in range(steps+1) :
					#print(i,j)
					fx = float(i) / (steps)
					fy = float(j) / (steps)
					px = x1 + (x2-x1) * fx
					py = y1 + (y2-y1) * fy 
					#print(px,py)
					#print(i,j,interpolatedPosition)
					focusValues = []
					self.moveTo(px,py,startingHeight+5,wait=True)
					for k in range(heightpoints):
						h = startingHeight - k * 1.0
						self.moveTo(px,py,h,wait=False)
						time.sleep(0.033) # Take some time for focus value to settle
						focusval = cam.getFocusValue()
						#print(focusval)
						focusValues.append( focusval )

					#print(focusValues)
					maxrank = numpy.argmax(focusValues)

					self.moveTo(px,py,startingHeight-maxrank*1.0,wait=True)

					# # TODO: Find max focus height position using curve fit 
					# poly_rank = 7
					# focusValues_indexes = range(len(focusValues))
					# polynomial = numpy.poly1d(numpy.polyfit(focusValues_indexes,focusValues,poly_rank))
					# numpts = 500
					# maxrank_high = numpy.argmax(polynomial(numpy.linspace(0, steps, numpts)))
					# maxrank = ( maxrank_high / (numpts-1) ) * steps
					# print(px,py,maxrank_high,maxrank)
					
					heights[i][j] = ( px,py, maxrank)

			# Bias results relative to initial point, at origin
			(x0,y0,home_rank) = heights[0][0]
			for i in range(steps+1) :
					for j in range(steps+1) :
						(x,y,r) = heights[i][j]
						x = x - x0
						y = y - y0
						r = r - home_rank
						heights[i][j] = (x,y,r)

			#print(heights)
			for col in heights :
				print(col)

			return heights

		return None

##################################################

class MicroscopeFeed:

	loopthread = None
	threadlock = None
	endLoopRequest = False
	focusValue = 0.0
	vidcap = None
	connected = False

	def __init__(self,channel):
		self.channel = channel
		self.threadlock = threading.Lock()
		self.loopthread = threading.Thread(target=self.loopThread)
		self.vidcap = cv2.VideoCapture(self.channel)
		if self.vidcap.isOpened() :
			self.connected = True
		else :
			print('Microscope connection could not be established.')

	def isConnected(self):
		return self.connected

	def startLoop(self):
		self.loopthread.start()

	def loopThread(self):
		if not self.vidcap.isOpened() : return
		smoothed_laplacian_variance = 0.0
		while True :
			chk,frame = self.vidcap.read()

			height, width = frame.shape[:2]
			sz = 0.20 * width
			x0 = int(width/2 - sz/2)
			x1 = int(width/2 + sz/2)
			y0 = int(height/2 - sz/2)
			y1 = int(height/2 + sz/2)
			center_frame = frame[ y0:y1, x0:x1 ]
			center_gray = cv2.cvtColor(center_frame, cv2.COLOR_BGR2GRAY)
			#cv2.imshow('center',center_gray)

			laplacian = cv2.Laplacian(center_gray,cv2.CV_64F)
			#cv2.imshow('laplacian',laplacian)
			v = laplacian.var()
			#smoothed_v_factor = 0.25
			smoothed_v_factor = 0.50
			smoothed_laplacian_variance = v * smoothed_v_factor +  smoothed_laplacian_variance * (1.0-smoothed_v_factor)
			#print('{:.0f} - {:.0f}'.format(v,smoothed_laplacian_variance))

			cv2.rectangle(frame, (x0, y0), (x1, y1),(0,255,0), 2)
			#textpos = (x0, y0)
			textpos = (10, 20)
			cv2.putText(frame, 'v = {:.2f} {:.2f}'.format(v,smoothed_laplacian_variance),textpos,cv2.FONT_HERSHEY_DUPLEX,0.8,(225,0,0))

			cv2.namedWindow('vidcap', cv2.WINDOW_NORMAL)
			cv2.imshow('vidcap',frame)
			cv2.waitKey(1) # Required for video to be displayed
			with self.threadlock :
				self.focusValue = smoothed_laplacian_variance
				if self.endLoopRequest :
					self.vidcap.release()
					cv2.destroyAllWindows() 
					break

	def endLoop(self):
		with self.threadlock :
			self.endLoopRequest = True
		self.loopthread.join()

	def getFocusValue(self):
		f = 0.0
		with self.threadlock :
			f = self.focusValue
		return f


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
	parser.add_option('--backlashX', dest='backlashX', default=0.0, help='Backlash compensation in X direction (in steps).')
	parser.add_option('--backlashY', dest='backlashY', default=0.0, help='Backlash compensation in y direction (in steps).')
	parser.add_option('--backlashZ', dest='backlashZ', default=0.0, help='Backlash compensation in z direction (in steps).')
	parser.add_option('--levelingsegments', dest='levelingsegments', default=1, help='Number of segments to split the work area for microscope-based leveling. (Default: 1)')
	parser.add_option('-m','--microscope', dest='microscope', default=False, help='Enable microscope on channel N')
	(options,args) = parser.parse_args()
	#print(options)

	debugmode = False

	# Find serial port number using the printer driver.
	serialport = ''
	if options.zero : # Printer driver is only required if we want to set the zero
		import subprocess
		shelloutput = subprocess.check_output('powershell -Command "(Get-WmiObject Win32_Printer -Filter \\"Name=\'{}\'\\").PortName"'.format(options.printerName))
		if len(shelloutput)>0 :
			try :
				serialport = shelloutput.decode('utf-8').split(':')[0]
				print( 'Found {} printer driver ({})'.format(options.printerName,serialport) )
			except:
				print('Error parsing com port: ' + str(shelloutput) )
		else :
			print('Could not find the printer driver for: ' + options.printerName)
			if not debugmode :
				sys.exit(1)

	# Start microscope feed if requested
	mic = None
	if options.microscope != False :
		mic = MicroscopeFeed( int(options.microscope) )
		mic.startLoop()

	#msvcrt.getwch()
	#print( mic.getFocusValue() )

	try:

		# Manually set zero and microscope set points
		x_offset = 0.0
		y_offset = 0.0
		modelaZeroControl = None
		manualLevelingPoints = None
		if options.zero :
			modelaZeroControl = ModelaZeroControl(serialport)
			if modelaZeroControl.connected or debugmode :
				print('Setting Zero')
				(x_offset,y_offset) = modelaZeroControl.run()
				manualLevelingPoints = modelaZeroControl.getManualLevelingPoints()
				if modelaZeroControl.exitRequested :
					print('Terminating program.')
					sys.exit(1)
			else :
				print('Could not connect to the printer to set the zero.')

		# Find bed level using microscope focus
		levelingData = None
		if mic != None and mic.isConnected() and modelaZeroControl != None :
			try:
				levelingData = modelaZeroControl.getAutolevelingData(mic, steps=int(options.levelingsegments) )
			except KeyboardInterrupt :
				print('Leveling cancelled, terminating program.')
				sys.exit(1)

		# gcode to rml conversion
		if options.infile != '' :
			if options.outfile == '' : options.outfile = options.infile + '.prn'
			print('Converting {} to {}'.format(options.infile,options.outfile))
			converter = GCode2RmlConverter(x_offset, y_offset, float(options.feedspeedfactor), float(options.backlashX), float(options.backlashY), float(options.backlashZ), levelingData, manualLevelingPoints )
			converter.convertFile( options.infile, options.outfile )


		# Send RML code to the printer driver.
		if options.print :
			if options.outfile != '' :
				print('Are you ready to print (y/n)?')
				c = msvcrt.getwch()
				if c == 'y' or c == 'Y' :
					print('Printing: '+options.outfile)
					os.system('RawFileToPrinter.exe "{}" "{}"'.format(options.outfile,options.printerName)) 

					print('Procedure to cancel printing:')
					print('1) Press the VIEW button on the printer.')
					print('2) Cancel the print job(s) in windows. (start->Devices and Printers->...)')
					print('3) Remove the usb cable to the printer.')
					print('4) Press both the UP and Down buttons on the printer.')
					print('5) When VIEW light stops blinking, press the VIEW butotn.')
					print('6) Plug the usb cable back in.')

				if mic != None and mic.isConnected() :
					# Don't exit now if the camera is connected, in case we want visual feedback
					print('Press any key to exit.')
					msvcrt.getwch()


			else :
				print('Error: No file to be printed.')

	except Exception as e: 
		#print(e)
		traceback.print_exc()

	# Release video stream
	if mic != None :
		mic.endLoop()



if __name__ == "__main__":
	if sys.version_info[0] < 3 :
		print("This script requires Python version 3")
		sys.exit(1)
	main()